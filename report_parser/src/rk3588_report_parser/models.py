from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Tuple


FIELD_NAMES: Tuple[str, ...] = (
    "patient_name",
    "patient_id",
    "sex",
    "age",
    "birthday",
    "his_exam_no",
    "report_no",
    "report_date",
    "exam_item",
)
IDENTIFIER_FIELDS = ("patient_id", "his_exam_no", "report_no")


@dataclass(frozen=True)
class OcrSpan:
    """An atomic OCR item retained as the only allowed field-value evidence."""

    id: int
    source_index: int
    line_id: int
    text: str
    box: Tuple[int, int, int, int]
    normalized_box: Tuple[int, int, int, int]
    score: float
    polygon: Tuple[Tuple[int, int], ...] = ()
    normalized_polygon: Tuple[Tuple[int, int], ...] = ()
    recognition_source: str = "primary"
    alternatives: Tuple[Dict[str, Any], ...] = ()

    @property
    def center(self) -> Tuple[float, float]:
        left, top, right, bottom = self.box
        return ((left + right) / 2.0, (top + bottom) / 2.0)

    def to_prompt_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "line": self.line_id,
            "text": self.text,
            "box": list(self.normalized_box),
            "score": round(self.score, 4),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "source_index": self.source_index,
            "line_id": self.line_id,
            "text": self.text,
            "box": list(self.box),
            "normalized_box": list(self.normalized_box),
            "score": round(self.score, 4),
            "polygon": [list(point) for point in self.polygon],
            "normalized_polygon": [list(point) for point in self.normalized_polygon],
            "recognition_source": self.recognition_source,
            "alternatives": [dict(item) for item in self.alternatives],
        }


@dataclass
class QualityAssessment:
    ok: bool
    image_size: Tuple[int, int]
    image_format: str
    metrics: Dict[str, float]
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "image_size": list(self.image_size),
            "image_format": self.image_format,
            "metrics": {key: round(value, 4) for key, value in self.metrics.items()},
            "reasons": list(self.reasons),
        }


@dataclass
class FieldEvidence:
    value: str = ""
    label_span_ids: List[int] = field(default_factory=list)
    source_span_ids: List[int] = field(default_factory=list)
    ocr_confidence: float = 0.0
    validation_ok: bool = True
    validation_reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "value": self.value,
            "label_span_ids": list(self.label_span_ids),
            "source_span_ids": list(self.source_span_ids),
            "ocr_confidence": round(self.ocr_confidence, 4),
            "validation": {
                "ok": self.validation_ok,
                "reasons": list(self.validation_reasons),
            },
        }


@dataclass
class ParseResult:
    version: str
    status: str
    image_sha256: str
    quality: QualityAssessment
    fields: Dict[str, FieldEvidence]
    rejection_reasons: List[str] = field(default_factory=list)
    ocr_summary: Dict[str, Any] = field(default_factory=dict)
    association: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "status": self.status,
            "image_sha256": self.image_sha256,
            "quality": self.quality.to_dict(),
            "fields": {name: self.fields[name].to_dict() for name in FIELD_NAMES},
            "ocr_summary": self.ocr_summary,
            "association": self.association,
            "rejection_reasons": list(self.rejection_reasons),
        }


def empty_fields() -> Dict[str, FieldEvidence]:
    return {name: FieldEvidence() for name in FIELD_NAMES}


def average_score(spans: Sequence[OcrSpan]) -> float:
    if not spans:
        return 0.0
    return sum(span.score for span in spans) / len(spans)
