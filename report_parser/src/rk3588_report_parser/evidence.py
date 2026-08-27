from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Mapping, Sequence

from .models import FIELD_NAMES, OcrSpan


@dataclass(frozen=True)
class EvidenceValidationResult:
    value_links: Dict[str, List[int]]
    label_links: Dict[str, List[int]]
    value_modes: Dict[str, str]
    reasons: List[str]


def extract_after_delimiter(text: str) -> str:
    match = re.search(r"[:\uFF1A]", text)
    if match is None:
        return ""
    label = text[: match.start()].strip()
    value = text[match.end() :].strip()
    return value if label and value else ""


def _first_value_is_near_label(label: OcrSpan, value: OcrSpan) -> bool:
    line_gap = value.line_id - label.line_id
    if line_gap == 0:
        tolerance = max(8, label.box[3] - label.box[1])
        return value.box[0] >= label.box[2] - tolerance
    return 1 <= line_gap <= 2


def _values_are_contiguous(values: Sequence[OcrSpan]) -> bool:
    if not values:
        return False
    for previous, current in zip(values, values[1:]):
        line_gap = current.line_id - previous.line_id
        if line_gap < 0 or line_gap > 1:
            return False
        if line_gap == 0 and current.box[0] < previous.box[0]:
            return False
    return True


def validate_model_evidence(
    value_links: Mapping[str, Sequence[int]],
    label_links: Mapping[str, Sequence[int]],
    value_modes: Mapping[str, str],
    spans: Sequence[OcrSpan],
) -> EvidenceValidationResult:
    """Validate model-selected evidence without understanding hospital labels."""

    by_id = {span.id: span for span in spans}
    accepted_values: Dict[str, List[int]] = {field: [] for field in FIELD_NAMES}
    accepted_labels: Dict[str, List[int]] = {field: [] for field in FIELD_NAMES}
    accepted_modes: Dict[str, str] = {field: "full_span" for field in FIELD_NAMES}
    reasons: List[str] = []
    used_by: Dict[int, str] = {}

    for field in FIELD_NAMES:
        values = list(value_links.get(field, []))
        labels = list(label_links.get(field, []))
        mode = str(value_modes.get(field, "full_span"))
        if not values:
            continue
        field_reasons: List[str] = []
        if len(labels) != 1:
            field_reasons.append("model_evidence_requires_one_label")
        if len(values) > (8 if field == "exam_item" else 1):
            field_reasons.append("too_many_value_spans")
        if mode not in {"full_span", "after_delimiter"}:
            field_reasons.append("invalid_value_mode")

        unknown_ids = [span_id for span_id in labels + values if span_id not in by_id]
        if unknown_ids:
            field_reasons.append("unknown_evidence_span")

        if not field_reasons:
            label = by_id[labels[0]]
            value_spans = [by_id[span_id] for span_id in values]
            if mode == "after_delimiter":
                if values != labels or not extract_after_delimiter(label.text):
                    field_reasons.append("invalid_same_span_evidence")
            else:
                if labels[0] in values:
                    field_reasons.append("full_span_reuses_label")
                elif not _first_value_is_near_label(label, value_spans[0]):
                    field_reasons.append("value_not_near_label")
                elif not _values_are_contiguous(value_spans):
                    field_reasons.append("value_spans_not_contiguous")

        for span_id in set(labels + values):
            previous = used_by.get(span_id)
            if previous is not None and previous != field:
                field_reasons.append("evidence_span_reused_by:%s" % previous)

        if field_reasons:
            reasons.extend("%s:%s" % (field, reason) for reason in sorted(set(field_reasons)))
            continue

        accepted_values[field] = values
        accepted_labels[field] = labels
        accepted_modes[field] = mode
        for span_id in set(labels + values):
            used_by[span_id] = field

    return EvidenceValidationResult(
        value_links=accepted_values,
        label_links=accepted_labels,
        value_modes=accepted_modes,
        reasons=sorted(set(reasons)),
    )
