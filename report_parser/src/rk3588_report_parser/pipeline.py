from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from PIL import Image, ImageDraw

from . import __version__
from .association import high_confidence_label_links
from .clients import FieldLinkerProtocol, LocalOpenAIChatClient, LocalPpOcrClient, OcrClientProtocol, ServiceError
from .evidence import validate_model_evidence
from .models import FIELD_NAMES, ParseResult, QualityAssessment, average_score, empty_fields
from .prompt import ModelResponseError, SYSTEM_PROMPT, build_user_prompt, parse_field_associations
from .quality import InvalidImageError, assess_image, decode_image, encode_for_ocr
from .settings import ParserSettings
from .spans import build_spans, spans_to_dicts
from .validation import materialize_and_validate


@dataclass
class ParseOutcome:
    result: ParseResult
    image: Optional[Image.Image]
    spans: Sequence[Any]
    model_response: str = ""


class ReportParser:
    def __init__(
        self,
        settings: ParserSettings,
        ocr_client: Optional[OcrClientProtocol] = None,
        linker: Optional[FieldLinkerProtocol] = None,
        association_mode: str = "model_only",
        target_fields: Optional[Sequence[str]] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        if association_mode not in {"model_only", "hybrid", "evidence"}:
            raise ValueError("association_mode must be model_only, hybrid, or evidence")
        self.settings = settings
        self.ocr_client = ocr_client or LocalPpOcrClient()
        self.linker = linker or LocalOpenAIChatClient()
        self.association_mode = association_mode
        requested = tuple(target_fields) if target_fields is not None else FIELD_NAMES
        unknown = set(requested) - set(FIELD_NAMES)
        if unknown or not requested:
            raise ValueError("target_fields must contain supported fields")
        self.target_fields = requested
        self.progress_callback = progress_callback

    def parse_path(self, path: Path) -> ParseOutcome:
        return self.parse_bytes(path.read_bytes())

    def parse_bytes(self, image_bytes: bytes) -> ParseOutcome:
        digest = hashlib.sha256(image_bytes).hexdigest()
        self._progress("checking image quality")
        try:
            image = decode_image(image_bytes)
        except InvalidImageError as exc:
            quality = QualityAssessment(
                ok=False,
                image_size=(0, 0),
                image_format="unknown",
                metrics={},
                reasons=[str(exc)],
            )
            return self._rejected(digest, quality, quality.reasons, None, [])

        quality = assess_image(image, self.settings.quality)
        if not quality.ok:
            return self._rejected(
                digest,
                quality,
                ["image_quality:%s" % reason for reason in quality.reasons],
                image,
                [],
            )

        self._progress("running local PP-OCR")
        response = self.ocr_client.recognize(encode_for_ocr(image), self.settings.ocr)
        return self._parse_ocr_response(response, image.size, digest, quality, image)

    def parse_ocr_response(
        self,
        response: Dict[str, Any],
        image_size: Sequence[int],
        image_sha256: Optional[str] = None,
    ) -> ParseOutcome:
        """Run the same model and validation path against saved OCR evidence.

        This is intentionally for desktop semantic evaluation only.  It skips
        the image-quality gate because the image itself is not part of the
        benchmark input, but it retains the OCR confidence gate, prompt,
        evidence reconstruction, and validation logic used by normal parsing.
        """

        if not isinstance(response, dict):
            raise ValueError("OCR response must be an object")
        if len(image_size) != 2:
            raise ValueError("image_size must contain width and height")
        try:
            width, height = int(image_size[0]), int(image_size[1])
        except (TypeError, ValueError) as exc:
            raise ValueError("image_size must contain integer dimensions") from exc
        if width < 1 or height < 1:
            raise ValueError("image_size dimensions must be positive")

        digest = image_sha256 or hashlib.sha256(
            json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        quality = QualityAssessment(
            ok=True,
            image_size=(width, height),
            image_format="ocr_fixture",
            metrics={},
            reasons=[],
        )
        return self._parse_ocr_response(response, (width, height), digest, quality, None)

    def _parse_ocr_response(
        self,
        response: Dict[str, Any],
        image_size: Sequence[int],
        digest: str,
        quality: QualityAssessment,
        image: Optional[Image.Image],
    ) -> ParseOutcome:
        if response.get("ok") is False:
            return self._rejected(digest, quality, ["ocr_service_failed"], image, [])
        if not isinstance(response.get("ocr"), list):
            return self._rejected(digest, quality, ["invalid_ocr_response"], image, [])

        spans = build_spans(response, (int(image_size[0]), int(image_size[1])))
        mean_score = average_score(spans)
        ocr_summary = {"item_count": len(spans), "mean_score": round(mean_score, 4)}
        self._progress("OCR complete: %d text spans, mean confidence %.3f" % (len(spans), mean_score))
        ocr_reasons: List[str] = []
        if len(spans) < self.settings.quality.min_ocr_items:
            ocr_reasons.append("insufficient_ocr_items")
        if mean_score < self.settings.quality.min_ocr_score:
            ocr_reasons.append("low_ocr_confidence")
        if ocr_reasons:
            return self._rejected(digest, quality, ocr_reasons, image, spans, ocr_summary)

        geometry_links = high_confidence_label_links(spans) if self.association_mode == "hybrid" else {}
        if geometry_links:
            self._progress("found %d unambiguous label/value links" % len(geometry_links))
        user_prompt = build_user_prompt(spans, fixed_links=geometry_links)
        model_response = ""
        model_response_error = ""
        model_response_valid = False
        try:
            self._progress("running field association")
            model_response = self.linker.link(SYSTEM_PROMPT, user_prompt, self.settings.llm)
            parsed_associations = parse_field_associations(model_response)
            model_links = parsed_associations.value_links
            model_label_links = parsed_associations.label_links
            model_value_modes = parsed_associations.value_modes
            model_response_valid = True
        except ModelResponseError as exc:
            model_response_error = str(exc)
            if self.association_mode in {"model_only", "evidence"}:
                return self._rejected(
                    digest,
                    quality,
                    ["model_response_invalid:%s" % exc],
                    image,
                    spans,
                    ocr_summary,
                )
            model_links = {field: [] for field in empty_fields()}
            model_label_links = {field: [] for field in FIELD_NAMES}
            model_value_modes = {field: "full_span" for field in FIELD_NAMES}

        association: Dict[str, Any] = {
            "mode": self.association_mode,
            "model_response_valid": model_response_valid,
            "label_geometry_fields": [],
        }
        if model_response_error:
            association["model_response_error"] = model_response_error
        links = model_links
        final_label_links: Dict[str, List[int]] = {field: [] for field in FIELD_NAMES}
        final_value_modes: Dict[str, str] = {field: "full_span" for field in FIELD_NAMES}
        evidence_reasons: List[str] = []
        if self.association_mode == "hybrid":
            # A syntactically valid OCR span is not enough proof that it has
            # the requested medical meaning. In the safe real-image mode,
            # only unambiguous label/value evidence can enter the final JSON.
            # Keep the model response for controlled diagnostics, but never
            # turn an unanchored model guess into an accepted patient field.
            label_fields = sorted(geometry_links)
            unanchored_model_fields = [
                field for field in FIELD_NAMES if model_links.get(field) and field not in geometry_links
            ]
            links = {field: list(geometry_links.get(field, [])) for field in FIELD_NAMES}
            association["label_geometry_fields"] = label_fields
            association["discarded_unanchored_model_fields"] = unanchored_model_fields
            if not model_response_valid and not label_fields:
                return self._rejected(
                    digest,
                    quality,
                    ["model_response_invalid:%s" % model_response_error],
                    image,
                    spans,
                    ocr_summary,
                )
        elif self.association_mode == "evidence":
            evidence = validate_model_evidence(
                model_links,
                model_label_links,
                model_value_modes,
                spans,
            )
            links = evidence.value_links
            final_label_links = evidence.label_links
            final_value_modes = evidence.value_modes
            evidence_reasons = evidence.reasons
            association["model_label_fields"] = [
                field for field in FIELD_NAMES if final_label_links.get(field)
            ]
            association["evidence_rejection_reasons"] = evidence_reasons

        target_set = set(self.target_fields)
        links = {
            field: list(links.get(field, [])) if field in target_set else []
            for field in FIELD_NAMES
        }
        final_label_links = {
            field: list(final_label_links.get(field, [])) if field in target_set else []
            for field in FIELD_NAMES
        }
        final_value_modes = {
            field: final_value_modes.get(field, "full_span") if field in target_set else "full_span"
            for field in FIELD_NAMES
        }

        self._progress("running final field validation")
        fields, rejection_reasons = materialize_and_validate(
            links,
            spans,
            self.settings.validation,
            label_links=final_label_links,
            value_modes=final_value_modes,
        )
        rejection_reasons = sorted(set(rejection_reasons + evidence_reasons))
        return ParseOutcome(
            result=ParseResult(
                version=__version__,
                status="accepted" if not rejection_reasons else "rejected",
                image_sha256=digest,
                quality=quality,
                fields=fields,
                ocr_summary=ocr_summary,
                rejection_reasons=rejection_reasons,
                association=association,
            ),
            image=image,
            spans=spans,
            model_response=model_response,
        )

    def _rejected(
        self,
        digest: str,
        quality: QualityAssessment,
        reasons: List[str],
        image: Optional[Image.Image],
        spans: Sequence[Any],
        ocr_summary: Optional[Dict[str, Any]] = None,
    ) -> ParseOutcome:
        return ParseOutcome(
            result=ParseResult(
                version=__version__,
                status="rejected",
                image_sha256=digest,
                quality=quality,
                fields=empty_fields(),
                ocr_summary=ocr_summary or {"item_count": len(spans), "mean_score": 0.0},
                rejection_reasons=sorted(set(reasons)),
            ),
            image=image,
            spans=spans,
        )

    def _progress(self, message: str) -> None:
        if self.progress_callback is not None:
            self.progress_callback(message)


def write_debug(outcome: ParseOutcome, debug_dir: Path) -> None:
    """Write explicit local diagnostics only when the caller opts in."""

    debug_dir.mkdir(parents=True, exist_ok=True)
    (debug_dir / "ocr_spans.json").write_text(
        json.dumps(spans_to_dicts(outcome.spans), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if outcome.model_response:
        (debug_dir / "model_response.txt").write_text(outcome.model_response, encoding="utf-8")
    if outcome.image is None:
        return
    overlay = outcome.image.copy()
    draw = ImageDraw.Draw(overlay)
    for span in outcome.spans:
        left, top, right, bottom = span.box
        draw.rectangle((left, top, right, bottom), outline=(230, 30, 30), width=max(2, overlay.width // 1000))
        draw.text((left, max(0, top - 18)), str(span.id), fill=(230, 30, 30))
    overlay.save(debug_dir / "ocr_overlay.jpg", format="JPEG", quality=92)
