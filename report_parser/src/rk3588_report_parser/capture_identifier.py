"""Privacy-preserving configured-length extraction for camera captures."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Optional, Protocol

from .capture_text import CapturedTextDocument, build_captured_text_document
from .identifier_pipeline import IdentifierParseOutcome, IdentifierParser
from .identifier_rules import parse_identifier_rule_settings, uses_character_count_only
from .settings import ParserSettings, load_settings, with_endpoint_overrides


class IdentifierParserProtocol(Protocol):
    def parse_bytes(self, image_bytes: bytes) -> IdentifierParseOutcome:
        ...


@dataclass(frozen=True)
class CaptureIdentifierExtraction:
    status: str
    value: Optional[str] = field(repr=False)
    parser_status: str
    identifier_count: int
    alternative_count: int
    ocr_item_count: int
    elapsed_ms: float
    reasons: tuple[str, ...] = ()
    document: Optional[CapturedTextDocument] = field(default=None, repr=False)

    @property
    def accepted(self) -> bool:
        return self.status == "accepted" and self.value is not None

    def public_status(self) -> Dict[str, Any]:
        return {
            "ready": True,
            "status": self.status,
            "value_available": self.value is not None,
            "parser_status": self.parser_status,
            "identifier_count": self.identifier_count,
            "alternative_count": self.alternative_count,
            "ocr_item_count": self.ocr_item_count,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "reasons": list(self.reasons),
            "full_text": (
                self.document.public_status()
                if self.document is not None
                else {"available": False, "line_count": 0, "item_count": 0, "mean_confidence": 0.0}
            ),
        }


class CaptureIdentifierExtractor:
    def __init__(self, parser: IdentifierParserProtocol) -> None:
        self.parser = parser

    def extract(self, image_bytes: bytes) -> CaptureIdentifierExtraction:
        started = time.monotonic()
        try:
            outcome = self.parser.parse_bytes(image_bytes)
        except Exception as exc:
            return CaptureIdentifierExtraction(
                status="error",
                value=None,
                parser_status="error",
                identifier_count=0,
                alternative_count=0,
                ocr_item_count=0,
                elapsed_ms=(time.monotonic() - started) * 1000.0,
                reasons=("parser_error:%s" % type(exc).__name__,),
                document=None,
            )

        result = outcome.result
        spans = tuple(getattr(outcome, "spans", ()))
        quality = getattr(result, "quality", None)
        image_size = getattr(quality, "image_size", (0, 0))
        document = (
            build_captured_text_document(spans, image_size)
            if spans and len(image_size) == 2 and all(int(value) > 0 for value in image_size)
            else None
        )
        primary = result.primary_identifier
        accepted = (
            result.status == "accepted"
            and primary is not None
            and primary.type == "selected_identifier"
            and bool(primary.value)
        )
        reasons = tuple(dict.fromkeys(result.review_reasons + result.rejection_reasons))
        return CaptureIdentifierExtraction(
            status="accepted" if accepted else result.status,
            value=primary.value if accepted else None,
            parser_status=result.status,
            identifier_count=len(result.identifiers),
            alternative_count=len(result.alternatives),
            ocr_item_count=int(result.ocr_summary.get("item_count", 0)),
            elapsed_ms=(time.monotonic() - started) * 1000.0,
            reasons=reasons or (() if accepted else ("identifier_not_uniquely_accepted",)),
            document=document,
        )


@dataclass(frozen=True)
class CaptureVerificationDecision:
    status: str
    reason: str
    attempt: int

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"

    def public_status(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "attempt": self.attempt,
        }


def decide_capture_retry(
    reason: str,
    attempt: int,
    max_attempts: int,
) -> CaptureVerificationDecision:
    if attempt < 1 or max_attempts < 1 or attempt > max_attempts:
        raise ValueError("capture verification attempt is invalid")
    return CaptureVerificationDecision(
        "retrying" if attempt < max_attempts else "rejected",
        reason,
        attempt + 1 if attempt < max_attempts else attempt,
    )


def verify_capture_pair(
    field_a: CaptureIdentifierExtraction,
    field_b: CaptureIdentifierExtraction,
    attempt: int,
    max_attempts: int,
) -> CaptureVerificationDecision:
    if attempt < 1 or max_attempts < 1 or attempt > max_attempts:
        raise ValueError("capture verification attempt is invalid")
    if field_a.accepted and field_b.accepted and field_a.value == field_b.value:
        return CaptureVerificationDecision("accepted", "exact_match", attempt)
    if not field_a.accepted:
        reason = "field_a_%s" % field_a.status
    elif not field_b.accepted:
        reason = "field_b_%s" % field_b.status
    else:
        reason = "identifier_mismatch"
    return decide_capture_retry(reason, attempt, max_attempts)


def load_capture_parser_settings(
    config_path: Path,
    rules_path: Optional[Path],
    ocr_endpoint: str,
    ocr_timeout: float,
) -> ParserSettings:
    settings = with_endpoint_overrides(load_settings(config_path), ocr_endpoint, None)
    settings = replace(settings, ocr=replace(settings.ocr, timeout_seconds=ocr_timeout))
    if rules_path is not None and rules_path.is_file():
        try:
            saved_rules = json.loads(rules_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("invalid identifier rules JSON: %s" % exc) from exc
        settings = replace(
            settings,
            identifier_rules=parse_identifier_rule_settings(saved_rules),
        )
    if not uses_character_count_only(settings.identifier_rules):
        raise ValueError("camera capture requires one or more selected_identifier length rules")
    return settings


def create_capture_identifier_extractor(
    settings: ParserSettings,
    ocr_client: Optional[Any] = None,
) -> CaptureIdentifierExtractor:
    return CaptureIdentifierExtractor(IdentifierParser(settings, ocr_client=ocr_client))
