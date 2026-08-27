from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

from PIL import Image, ImageDraw

from . import __version__
from .clients import LocalPpOcrClient, OcrClientProtocol
from .identifier_adjudication import adjudicate_identifiers
from .identifier_candidates import build_identifier_candidates
from .identifier_linker import BatchIdentifierLinker, BatchLinkOutcome
from .identifier_models import IdentifierCandidate, IdentifierParseResult
from .identifier_rules import configured_primary_priority, uses_character_count_only
from .models import QualityAssessment, average_score
from .ocr_refinement import refine_configured_identifier_ocr
from .quality import InvalidImageError, assess_image, decode_image, encode_for_ocr
from .preprocessing import prepare_for_ocr, restore_ocr_coordinates
from .settings import ParserSettings
from .rule_linker import link_identifiers_with_rules
from .rule_candidates import build_rule_identifier_candidates
from .spans import build_spans, spans_to_dicts


@dataclass
class IdentifierParseOutcome:
    result: IdentifierParseResult
    image: Optional[Image.Image]
    spans: Sequence[Any]
    candidates: Sequence[IdentifierCandidate]
    classification_response: str = ""
    verification_response: str = ""


class IdentifierParser:
    def __init__(
        self,
        settings: ParserSettings,
        ocr_client: Optional[OcrClientProtocol] = None,
        linker: Optional[BatchIdentifierLinker] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.settings = settings
        self.ocr_client = ocr_client or LocalPpOcrClient()
        self.linker = linker or BatchIdentifierLinker()
        self.progress_callback = progress_callback

    def parse_path(self, path: Path) -> IdentifierParseOutcome:
        return self.parse_bytes(path.read_bytes())

    def parse_bytes(self, image_bytes: bytes) -> IdentifierParseOutcome:
        total_started = time.monotonic()
        digest = hashlib.sha256(image_bytes).hexdigest()
        self._progress("checking image quality")
        try:
            image = decode_image(image_bytes)
        except InvalidImageError as exc:
            quality = QualityAssessment(False, (0, 0), "unknown", {}, [str(exc)])
            return self._rejected(digest, quality, ["invalid_image"], None, (), total_started)

        quality = assess_image(image, self.settings.quality)
        if not quality.ok and not uses_character_count_only(self.settings.identifier_rules):
            return self._rejected(
                digest,
                quality,
                ["image_quality:%s" % reason for reason in quality.reasons],
                image,
                (),
                total_started,
            )

        prepared = prepare_for_ocr(image, self.settings.preprocessing)
        quality.metrics["perspective_corrected"] = 1.0 if prepared.applied else 0.0
        quality.metrics["perspective_confidence"] = prepared.confidence
        self._progress("running local PP-OCR")
        ocr_started = time.monotonic()
        response = self.ocr_client.recognize(encode_for_ocr(prepared.image), self.settings.ocr)
        if uses_character_count_only(self.settings.identifier_rules):
            response = refine_configured_identifier_ocr(
                prepared.image,
                response,
                self.settings.identifier_rules,
                lambda crop: self.ocr_client.recognize(encode_for_ocr(crop), self.settings.ocr),
            )
        response = restore_ocr_coordinates(response, prepared.inverse_transform, image.size)
        ocr_ms = (time.monotonic() - ocr_started) * 1000.0
        return self._parse_ocr_response(response, image, digest, quality, ocr_ms, total_started)

    def parse_ocr_response(
        self,
        response: Dict[str, Any],
        image_size: Sequence[int],
        image_sha256: Optional[str] = None,
    ) -> IdentifierParseOutcome:
        if len(image_size) != 2 or int(image_size[0]) < 1 or int(image_size[1]) < 1:
            raise ValueError("image_size must contain positive width and height")
        digest = image_sha256 or hashlib.sha256(
            json.dumps(response, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        quality = QualityAssessment(True, (int(image_size[0]), int(image_size[1])), "ocr_fixture", {}, [])
        total_started = time.monotonic()
        return self._parse_ocr_response(response, None, digest, quality, 0.0, total_started)

    def _parse_ocr_response(
        self,
        response: Dict[str, Any],
        image: Optional[Image.Image],
        digest: str,
        quality: QualityAssessment,
        ocr_ms: float,
        total_started: float,
    ) -> IdentifierParseOutcome:
        if response.get("ok") is False or not isinstance(response.get("ocr"), list):
            return self._rejected(
                digest, quality, ["ocr_service_failed"], image, (), total_started, ocr_ms=ocr_ms
            )
        spans = build_spans(response, quality.image_size)
        mean_score = average_score(spans)
        ocr_summary = {"item_count": len(spans), "mean_score": round(mean_score, 4)}
        refinement = response.get("refinement")
        if isinstance(refinement, dict):
            ocr_summary["refinement"] = {
                "mode": str(refinement.get("mode") or "")[:32],
                "calls": max(0, int(refinement.get("calls") or 0)),
                "recovered_values": max(0, int(refinement.get("recovered_values") or 0)),
            }
        self._progress("OCR complete: %d spans" % len(spans))
        ocr_reasons = []
        if len(spans) < self.settings.quality.min_ocr_items:
            ocr_reasons.append("insufficient_ocr_items")
        if mean_score < self.settings.quality.min_ocr_score:
            ocr_reasons.append("low_ocr_confidence")
        if ocr_reasons and not uses_character_count_only(self.settings.identifier_rules):
            return self._rejected(
                digest,
                quality,
                ocr_reasons,
                image,
                spans,
                total_started,
                ocr_ms=ocr_ms,
                ocr_summary=ocr_summary,
            )

        candidate_started = time.monotonic()
        if self.settings.identifier_rules.enabled:
            candidates = build_rule_identifier_candidates(
                spans,
                self.settings.identifier_rules,
                self.settings.identifiers,
            )
        else:
            candidates = build_identifier_candidates(spans, self.settings.identifiers)
        candidate_ms = (time.monotonic() - candidate_started) * 1000.0
        self._progress("built %d identifier candidates" % len(candidates))
        if not candidates:
            return self._rejected(
                digest,
                quality,
                ["no_identifier_candidates"],
                image,
                spans,
                total_started,
                ocr_ms=ocr_ms,
                candidate_ms=candidate_ms,
                ocr_summary=ocr_summary,
                candidates=candidates,
            )

        self._progress(
            "running OCR identifier rules"
            if self.settings.identifier_rules.enabled
            else "running batch identifier classification"
        )
        link_outcome: BatchLinkOutcome = link_identifiers_with_rules(
            self.linker,
            candidates,
            self.settings.llm,
            self.settings.identifier_rules,
        )
        self._progress("running deterministic identifier adjudication")
        adjudication_started = time.monotonic()
        status, primary, identifiers, alternatives, review_reasons, rejection_reasons = adjudicate_identifiers(
            link_outcome.candidates,
            self.settings.identifiers,
            configured_primary_priority(self.settings.identifier_rules),
            include_all_values=self.settings.identifier_rules.enabled,
            require_single_value=(
                self.settings.identifier_rules.enabled
                and bool(self.settings.identifier_rules.fields)
                and all(
                    rule.identifier_type == "selected_identifier"
                    for rule in self.settings.identifier_rules.fields
                    if rule.enabled
                )
            ),
        )
        adjudication_ms = (time.monotonic() - adjudication_started) * 1000.0
        total_ms = (time.monotonic() - total_started) * 1000.0
        result = IdentifierParseResult(
            version=__version__,
            status=status,
            image_sha256=digest,
            quality=quality,
            primary_identifier=primary,
            identifiers=identifiers,
            alternatives=alternatives,
            review_reasons=review_reasons,
            rejection_reasons=rejection_reasons,
            timings={
                "ocr_ms": ocr_ms,
                "candidate_ms": candidate_ms,
                "classification_ms": link_outcome.classification_ms,
                "verification_ms": link_outcome.verification_ms,
                "adjudication_ms": adjudication_ms,
                "total_ms": total_ms,
            },
            engine=self._engine(),
            ocr_summary=ocr_summary,
        )
        return IdentifierParseOutcome(
            result=result,
            image=image,
            spans=spans,
            candidates=candidates,
            classification_response=link_outcome.classification_response,
            verification_response=link_outcome.verification_response,
        )

    def _rejected(
        self,
        digest,
        quality,
        reasons,
        image,
        spans,
        total_started,
        ocr_ms=0.0,
        candidate_ms=0.0,
        ocr_summary=None,
        candidates=(),
    ):
        result = IdentifierParseResult(
            version=__version__,
            status="rejected",
            image_sha256=digest,
            quality=quality,
            primary_identifier=None,
            identifiers=[],
            alternatives=[],
            rejection_reasons=sorted(set(reasons)),
            timings={
                "ocr_ms": ocr_ms,
                "candidate_ms": candidate_ms,
                "classification_ms": 0.0,
                "verification_ms": 0.0,
                "adjudication_ms": 0.0,
                "total_ms": (time.monotonic() - total_started) * 1000.0,
            },
            engine=self._engine(),
            ocr_summary=ocr_summary or {"item_count": len(spans), "mean_score": 0.0},
        )
        return IdentifierParseOutcome(result, image, spans, candidates)

    def _engine(self) -> Dict[str, Any]:
        return {
            "profile": self.settings.profile,
            "ocr": "local_ppocr",
            "model": "disabled" if self.settings.identifier_rules.enabled else self.settings.llm.model,
            "linker": "ocr_rule_v1" if self.settings.identifier_rules.enabled else "batch_identifier_v1",
            "identifier_rules": self.settings.identifier_rules.summary(),
        }

    def _progress(self, message: str) -> None:
        if self.progress_callback is not None:
            self.progress_callback(message)


def write_identifier_debug(outcome: IdentifierParseOutcome, debug_dir: Path) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    (debug_dir / "ocr_spans.json").write_text(
        json.dumps(spans_to_dicts(outcome.spans), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (debug_dir / "identifier_candidates.json").write_text(
        json.dumps([candidate.to_prompt_dict() for candidate in outcome.candidates], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if outcome.classification_response:
        (debug_dir / "classification_response.txt").write_text(
            outcome.classification_response, encoding="utf-8"
        )
    if outcome.verification_response:
        (debug_dir / "verification_response.txt").write_text(
            outcome.verification_response, encoding="utf-8"
        )
    if outcome.image is None:
        return
    overlay = outcome.image.copy()
    draw = ImageDraw.Draw(overlay)
    for span in outcome.spans:
        draw.rectangle(span.box, outline=(215, 48, 39), width=max(2, overlay.width // 1000))
        draw.text((span.box[0], max(0, span.box[1] - 18)), str(span.id), fill=(215, 48, 39))
    overlay.save(debug_dir / "ocr_overlay.jpg", format="JPEG", quality=92)
