from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .models import QualityAssessment


CORE_IDENTIFIER_TYPES: Tuple[str, ...] = (
    "selected_identifier",
    "patient_id",
    "inpatient_no",
    "outpatient_no",
    "visit_no",
    "exam_request_no",
    "exam_no",
    "imaging_no",
    "medical_card_no",
)
OTHER_IDENTIFIER_TYPE = "other_medical_id"
UNKNOWN_IDENTIFIER_TYPE = "unknown_identifier"
IDENTIFIER_TYPES: Tuple[str, ...] = CORE_IDENTIFIER_TYPES + (OTHER_IDENTIFIER_TYPE, UNKNOWN_IDENTIFIER_TYPE)
MODEL_IDENTIFIER_TYPES: Tuple[str, ...] = CORE_IDENTIFIER_TYPES + (OTHER_IDENTIFIER_TYPE, "ignore")

PRIMARY_PRIORITY: Tuple[str, ...] = (
    "selected_identifier",
    "patient_id",
    "inpatient_no",
    "outpatient_no",
    "visit_no",
    "exam_request_no",
    "exam_no",
    "imaging_no",
    "medical_card_no",
)

IDENTIFIER_LABELS: Dict[str, str] = {
    "selected_identifier": "目标号码",
    "patient_id": "患者ID",
    "inpatient_no": "住院号",
    "outpatient_no": "门诊号",
    "visit_no": "就诊号",
    "exam_request_no": "检查申请号",
    "exam_no": "检查号",
    "imaging_no": "影像号",
    "medical_card_no": "就诊卡号",
    "other_medical_id": "其他医疗号码",
    "unknown_identifier": "未知号码",
}

RELATION_RANK: Dict[str, int] = {
    "same_span": 0,
    "same_line_right": 1,
    "next_line_aligned": 2,
    "nearby": 3,
    "unlabeled": 4,
}


@dataclass(frozen=True)
class IdentifierCandidate:
    id: int
    raw_label: str
    value: str
    label_span_ids: Tuple[int, ...]
    value_span_ids: Tuple[int, ...]
    value_mode: str
    relation: str
    normalized_distance: float
    ocr_confidence: float
    reading_order: int
    label_box: Tuple[int, int, int, int]
    value_boxes: Tuple[Tuple[int, int, int, int], ...]

    def ranking_key(self) -> Tuple[int, float, float, int, int]:
        return (
            RELATION_RANK[self.relation],
            -self.ocr_confidence,
            self.normalized_distance,
            self.reading_order,
            self.id,
        )

    def to_prompt_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.id,
            "label_text": self.raw_label,
            "value_text": self.value,
            "relation": self.relation,
            "label_span_ids": list(self.label_span_ids),
            "value_span_ids": list(self.value_span_ids),
        }


@dataclass(frozen=True)
class ClassifiedCandidate:
    candidate: IdentifierCandidate
    identifier_type: str
    confirmed: bool
    reasons: Tuple[str, ...] = ()
    review_reasons: Tuple[str, ...] = ()
    decision_source: str = "model"


@dataclass
class IdentifierEvidence:
    type: str
    value: str
    raw_label: str
    label_span_ids: List[int]
    value_span_ids: List[int]
    ocr_confidence: float
    relation: str
    normalized_distance: float
    label_box: List[int]
    value_boxes: List[List[int]]
    selected_for_type: bool
    is_primary: bool = False
    validation_ok: bool = True
    validation_reasons: List[str] = field(default_factory=list)
    decision_source: str = "model"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "type_label": IDENTIFIER_LABELS[self.type],
            "value": self.value,
            "raw_label": self.raw_label,
            "label_span_ids": list(self.label_span_ids),
            "value_span_ids": list(self.value_span_ids),
            "ocr_confidence": round(self.ocr_confidence, 4),
            "evidence": {
                "relation": self.relation,
                "normalized_distance": round(self.normalized_distance, 4),
                "label_box": list(self.label_box),
                "value_boxes": [list(box) for box in self.value_boxes],
            },
            "selected_for_type": self.selected_for_type,
            "is_primary": self.is_primary,
            "decision_source": self.decision_source,
            "validation": {
                "ok": self.validation_ok,
                "reasons": list(self.validation_reasons),
            },
        }


@dataclass
class IdentifierParseResult:
    version: str
    status: str
    image_sha256: str
    quality: QualityAssessment
    primary_identifier: Optional[IdentifierEvidence]
    identifiers: List[IdentifierEvidence]
    alternatives: List[IdentifierEvidence]
    review_reasons: List[str] = field(default_factory=list)
    rejection_reasons: List[str] = field(default_factory=list)
    timings: Dict[str, float] = field(default_factory=dict)
    engine: Dict[str, Any] = field(default_factory=dict)
    ocr_summary: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "status": self.status,
            "identifier": self.primary_identifier.value if self.primary_identifier is not None else None,
            "primary_identifier": (
                {
                    "type": self.primary_identifier.type,
                    "type_label": IDENTIFIER_LABELS[self.primary_identifier.type],
                    "value": self.primary_identifier.value,
                }
                if self.primary_identifier is not None
                else None
            ),
            "identifiers": [item.to_dict() for item in self.identifiers],
            "alternatives": [item.to_dict() for item in self.alternatives],
            "quality": self.quality.to_dict(),
            "ocr_summary": dict(self.ocr_summary),
            "review_reasons": list(self.review_reasons),
            "rejection_reasons": list(self.rejection_reasons),
            "timings": {key: round(value, 2) for key, value in self.timings.items()},
            "engine": dict(self.engine),
            "image_sha256": self.image_sha256,
        }
