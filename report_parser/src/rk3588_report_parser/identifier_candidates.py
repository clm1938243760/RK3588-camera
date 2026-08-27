from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .identifier_models import IdentifierCandidate, RELATION_RANK
from .models import OcrSpan


IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{3,63}$")
PHONE_PATTERNS = (
    re.compile(r"^1[3-9]\d{9}$"),
    re.compile(r"^0\d{2,3}-?\d{7,8}$"),
)
DATE_PATTERNS = (
    re.compile(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$"),
    re.compile(r"^\d{4}年\d{1,2}月\d{1,2}日?$"),
    re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$"),
)
EXCLUDED_LABEL_TERMS = (
    "电话",
    "手机",
    "身份证",
    "证件号",
    "医生工号",
    "医师工号",
    "金额",
    "费用",
    "年龄",
    "日期",
    "时间",
    "邮编",
    "床号",
)


@dataclass(frozen=True)
class CandidateSettings:
    max_candidates: int = 96
    minimum_ocr_score: float = 0.65
    same_line_gap: float = 220.0
    next_line_gap: float = 120.0
    nearby_distance: float = 220.0
    tie_confidence_delta: float = 0.03
    tie_distance_delta: float = 0.02


@dataclass(frozen=True)
class _ValueGroup:
    span_ids: Tuple[int, ...]
    value: str
    boxes: Tuple[Tuple[int, int, int, int], ...]
    line_id: int
    score: float
    reading_order: int


def compact_identifier(text: str) -> str:
    return re.sub(r"\s+", "", text or "").strip(";；,，")


def is_identifier_value(text: str) -> bool:
    value = compact_identifier(text)
    if not IDENTIFIER_PATTERN.fullmatch(value):
        return False
    if sum(character.isdigit() for character in value) < 2:
        return False
    if any(pattern.fullmatch(value) for pattern in DATE_PATTERNS):
        return False
    if any(pattern.fullmatch(value) for pattern in PHONE_PATTERNS):
        return False
    if re.fullmatch(r"\d{1,3}(?:\.\d+)?", value):
        return False
    return True


def _split_label_value(text: str) -> Optional[Tuple[str, str]]:
    match = re.search(r"[:：#]", text)
    if match is None:
        return None
    label = text[: match.start()].strip()
    value = compact_identifier(text[match.end() :])
    if not label or not is_identifier_value(value):
        return None
    return label, value


def _is_excluded_label(text: str) -> bool:
    compact = re.sub(r"\s+", "", text).lower()
    return any(term.lower() in compact for term in EXCLUDED_LABEL_TERMS)


def _looks_like_label(span: OcrSpan) -> bool:
    if _is_excluded_label(span.text):
        return False
    if _split_label_value(span.text) is not None:
        return True
    if is_identifier_value(span.text):
        return False
    return bool(re.search(r"[A-Za-z\u3400-\u9fff]", span.text)) and len(span.text) <= 48


def _normalized_gap(left: OcrSpan, right: OcrSpan) -> float:
    return float(max(0, right.normalized_box[0] - left.normalized_box[2]))


def _centroid(boxes: Sequence[Tuple[int, int, int, int]]) -> Tuple[float, float]:
    left = min(box[0] for box in boxes)
    top = min(box[1] for box in boxes)
    right = max(box[2] for box in boxes)
    bottom = max(box[3] for box in boxes)
    return ((left + right) / 2.0, (top + bottom) / 2.0)


def _distance(label: OcrSpan, value: _ValueGroup) -> float:
    label_center = _centroid((label.normalized_box,))
    value_center = _centroid(value.boxes)
    return math.dist(label_center, value_center) / 1000.0


def _horizontal_overlap(left: Tuple[int, int, int, int], right: Tuple[int, int, int, int]) -> int:
    return max(0, min(left[2], right[2]) - max(left[0], right[0]))


def _value_groups(spans: Sequence[OcrSpan], settings: CandidateSettings) -> List[_ValueGroup]:
    atomic = [
        _ValueGroup(
            span_ids=(span.id,),
            value=compact_identifier(span.text),
            boxes=(span.normalized_box,),
            line_id=span.line_id,
            score=span.score,
            reading_order=span.id,
        )
        for span in spans
        if span.score >= settings.minimum_ocr_score and is_identifier_value(span.text)
    ]
    by_id = {span.id: span for span in spans}
    combined: List[_ValueGroup] = []
    ordered = sorted(atomic, key=lambda item: (item.line_id, item.reading_order))
    for first, second in zip(ordered, ordered[1:]):
        if first.line_id != second.line_id:
            continue
        first_span = by_id[first.span_ids[0]]
        second_span = by_id[second.span_ids[0]]
        if _normalized_gap(first_span, second_span) > 30:
            continue
        joined = compact_identifier(first.value + second.value)
        if not is_identifier_value(joined):
            continue
        combined.append(
            _ValueGroup(
                span_ids=first.span_ids + second.span_ids,
                value=joined,
                boxes=first.boxes + second.boxes,
                line_id=first.line_id,
                score=(first.score + second.score) / 2.0,
                reading_order=first.reading_order,
            )
        )
    return atomic + combined


def _relation(label: OcrSpan, value: _ValueGroup, settings: CandidateSettings) -> Optional[str]:
    first_box = value.boxes[0]
    line_delta = value.line_id - label.line_id
    if line_delta == 0 and first_box[0] >= label.normalized_box[2] - 20:
        if first_box[0] - label.normalized_box[2] <= settings.same_line_gap:
            return "same_line_right"
    if line_delta == 1:
        vertical_gap = max(0, first_box[1] - label.normalized_box[3])
        aligned = _horizontal_overlap(label.normalized_box, first_box) > 0 or abs(
            _centroid((label.normalized_box,))[0] - _centroid(value.boxes)[0]
        ) <= 180
        if aligned and vertical_gap <= settings.next_line_gap:
            return "next_line_aligned"
    if 0 <= line_delta <= 2 and _distance(label, value) * 1000 <= settings.nearby_distance:
        return "nearby"
    return None


def build_identifier_candidates(
    spans: Sequence[OcrSpan],
    settings: Optional[CandidateSettings] = None,
) -> List[IdentifierCandidate]:
    config = settings or CandidateSettings()
    candidates: List[IdentifierCandidate] = []
    seen = set()

    for span in spans:
        if span.score < config.minimum_ocr_score:
            continue
        combined = _split_label_value(span.text)
        if combined is None or _is_excluded_label(combined[0]):
            continue
        label, value = combined
        key = ((span.id,), (span.id,), "after_delimiter")
        seen.add(key)
        candidates.append(
            IdentifierCandidate(
                id=0,
                raw_label=label,
                value=value,
                label_span_ids=(span.id,),
                value_span_ids=(span.id,),
                value_mode="after_delimiter",
                relation="same_span",
                normalized_distance=0.0,
                ocr_confidence=span.score,
                reading_order=span.id,
                label_box=span.normalized_box,
                value_boxes=(span.normalized_box,),
            )
        )

    labels = [span for span in spans if span.score >= config.minimum_ocr_score and _looks_like_label(span)]
    groups = _value_groups(spans, config)
    for label in labels:
        for value in groups:
            if label.id in value.span_ids:
                continue
            relation = _relation(label, value, config)
            if relation is None:
                continue
            key = ((label.id,), value.span_ids, "full_span")
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                IdentifierCandidate(
                    id=0,
                    raw_label=label.text,
                    value=value.value,
                    label_span_ids=(label.id,),
                    value_span_ids=value.span_ids,
                    value_mode="full_span",
                    relation=relation,
                    normalized_distance=_distance(label, value),
                    ocr_confidence=(label.score + value.score) / 2.0,
                    reading_order=value.reading_order,
                    label_box=label.normalized_box,
                    value_boxes=value.boxes,
                )
            )

    represented_value_spans = {candidate.value_span_ids for candidate in candidates}
    for value in groups:
        if value.span_ids in represented_value_spans:
            continue
        candidates.append(
            IdentifierCandidate(
                id=0,
                raw_label="",
                value=value.value,
                label_span_ids=(),
                value_span_ids=value.span_ids,
                value_mode="full_span",
                relation="unlabeled",
                normalized_distance=1.0,
                ocr_confidence=value.score,
                reading_order=value.reading_order,
                label_box=value.boxes[0],
                value_boxes=value.boxes,
            )
        )

    candidates.sort(
        key=lambda item: (
            RELATION_RANK[item.relation],
            item.normalized_distance,
            -item.ocr_confidence,
            item.reading_order,
            item.label_span_ids,
            item.value_span_ids,
        )
    )
    limited = candidates[: config.max_candidates]
    return [
        IdentifierCandidate(
            id=index,
            raw_label=item.raw_label,
            value=item.value,
            label_span_ids=item.label_span_ids,
            value_span_ids=item.value_span_ids,
            value_mode=item.value_mode,
            relation=item.relation,
            normalized_distance=item.normalized_distance,
            ocr_confidence=item.ocr_confidence,
            reading_order=item.reading_order,
            label_box=item.label_box,
            value_boxes=item.value_boxes,
        )
        for index, item in enumerate(limited, start=1)
    ]
