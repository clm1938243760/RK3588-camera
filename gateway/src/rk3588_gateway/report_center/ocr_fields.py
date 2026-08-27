from __future__ import annotations

import re
from dataclasses import dataclass
from math import hypot
from typing import Any, Optional, Protocol

from .domain import ValidationError, canonical_patient


@dataclass(frozen=True)
class FieldEvidence:
    field_key: str
    value: str
    span_ids: list[int]
    score: float
    relation: str
    label: str
    alternatives: list[dict[str, Any]]


class FieldResolverProvider(Protocol):
    @property
    def available(self) -> bool:
        ...

    async def resolve(self, spans: list[dict[str, Any]], schema: list[dict[str, Any]]) -> dict[str, list[int]]:
        ...


class DisabledModelFieldResolver:
    @property
    def available(self) -> bool:
        return False

    async def resolve(self, spans: list[dict[str, Any]], schema: list[dict[str, Any]]) -> dict[str, list[int]]:
        raise ValidationError("model field resolver is not available on this device")


class RuleFieldResolver:
    def resolve(self, ocr_payload: dict[str, Any], field_schema: list[dict[str, Any]]) -> dict[str, Any]:
        spans = _spans_from_payload(ocr_payload)
        evidence: dict[str, Any] = {}
        missing: list[str] = []
        conflicts: list[str] = []
        patient: dict[str, Any] = {}
        extra_fields: dict[str, Any] = {}

        for definition in field_schema:
            if not bool(definition.get("enabled", True)):
                continue
            item = self._resolve_field(spans, definition)
            key = str(definition.get("field_key", "")).strip()
            if not key:
                raise ValidationError("OCR field is missing field_key")
            if item is None:
                if bool(definition.get("required", False)):
                    missing.append(key)
                continue
            evidence[key] = {
                "value": item.value,
                "span_ids": item.span_ids,
                "score": item.score,
                "relation": item.relation,
                "label": item.label,
                "alternatives": item.alternatives,
            }
            if item.alternatives:
                conflicts.append(key)
            target = str(definition.get("target", key))
            if target in canonical_patient({}).keys() and target != "extra_fields":
                patient[target] = item.value
            else:
                extra_fields[target] = item.value
        patient["extra_fields"] = extra_fields
        status = "accepted" if evidence and not missing and not conflicts else "review_required"
        return {
            "status": status,
            "patient": canonical_patient(patient),
            "evidence": evidence,
            "missing_fields": missing,
            "conflict_fields": conflicts,
        }

    def _resolve_field(self, spans: list[dict[str, Any]], definition: dict[str, Any]) -> Optional[FieldEvidence]:
        if str(definition.get("match_mode", "")) == "fixed_roi":
            return self._resolve_fixed_roi(spans, definition)
        labels = [str(value).strip() for value in definition.get("label_aliases", []) if str(value).strip()]
        relation = str(definition.get("relation", "same_text"))
        allowed_relations = definition.get("relations", [relation, "same_line_right", "next_line_same_column", "nearest"])
        allowed = {str(value) for value in allowed_relations}
        candidates: list[tuple[float, dict[str, Any], str, str, list[int], float, str]] = []
        minimum_score = float(definition.get("min_ocr_score", 0.0))
        maximum_distance = float(definition.get("max_distance", 0.25))

        for span in spans:
            text = str(span.get("text", "")).strip()
            if float(span.get("score", 0.0)) < minimum_score:
                continue
            matched_label = _match_fixed_label(text, labels)
            if matched_label and "same_text" in allowed:
                suffix = text[len(matched_label):].lstrip(" :：=-")
                value = _validate_value(suffix, definition)
                if value:
                    candidates.append((0.0, span, value, "same_text", [int(span["id"])], float(span.get("score", 0)), matched_label))
            if not matched_label:
                continue
            for other in spans:
                if other is span or float(other.get("score", 0.0)) < minimum_score:
                    continue
                current_relation, distance = _relation(span, other)
                if current_relation not in allowed or distance > maximum_distance:
                    continue
                value = _validate_value(str(other.get("text", "")).strip(), definition)
                if value:
                    relation_rank = {"same_line_right": 1.0, "next_line_same_column": 2.0, "nearest": 3.0}.get(current_relation, 4.0)
                    candidates.append((relation_rank + distance, other, value, current_relation, [int(span["id"]), int(other["id"])], min(float(span.get("score", 0)), float(other.get("score", 0))), matched_label))

        if not labels:
            for span in spans:
                value = _validate_value(str(span.get("text", "")).strip(), definition)
                if value and _inside_roi(span, definition.get("roi")):
                    candidates.append((3.0, span, value, "roi" if definition.get("roi") else "nearest", [int(span["id"])], float(span.get("score", 0)), ""))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], -item[5], int(item[1]["id"])))
        best = candidates[0]
        alternatives = [
            {"value": item[2], "span_ids": item[4], "score": item[5], "relation": item[3]}
            for item in candidates[1:]
            if item[2] != best[2] and abs(item[0] - best[0]) < 0.25 and abs(item[5] - best[5]) < 0.03
        ]
        return FieldEvidence(
            field_key=str(definition["field_key"]), value=best[2], span_ids=best[4], score=best[5],
            relation=best[3], label=best[6],
            alternatives=alternatives,
        )

    def _resolve_fixed_roi(
        self,
        spans: list[dict[str, Any]],
        definition: dict[str, Any],
    ) -> Optional[FieldEvidence]:
        field_key = str(definition.get("field_key", "")).strip()
        roi = definition.get("roi")
        if not field_key or not roi:
            return None
        minimum_score = float(definition.get("min_ocr_score", 0.0))
        source_name = "roi:%s" % field_key
        candidates = [
            span for span in spans
            if float(span.get("score", 0.0)) >= minimum_score
            and (
                str(span.get("recognition_source", "")) == source_name
                or _inside_roi(span, roi)
            )
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda span: (
                0 if str(span.get("recognition_source", "")) == source_name else 1,
                int(span.get("line_id", 0)),
                _box(span)[0],
                int(span.get("id", 0)),
            )
        )
        labels = [
            str(value).strip()
            for value in definition.get("label_aliases", [])
            if str(value).strip()
        ]
        join_mode = str(definition.get("join_mode", "single"))
        if join_mode == "reading_order":
            pieces = [
                _strip_roi_label(str(span.get("text", "")), labels)
                for span in candidates
            ]
            pieces = [value for value in pieces if value]
            separator = str(definition.get("join_separator", ""))[:8]
            value = _validate_value(separator.join(pieces), definition)
            if not value:
                return None
            return FieldEvidence(
                field_key=field_key,
                value=value,
                span_ids=[int(span["id"]) for span in candidates],
                score=min(float(span.get("score", 0.0)) for span in candidates),
                relation="fixed_roi_join",
                label="",
                alternatives=[],
            )

        valid = []
        for span in candidates:
            raw = _strip_roi_label(str(span.get("text", "")), labels)
            value = _validate_value(raw, definition)
            if value:
                valid.append((span, value))
        if not valid:
            return None
        valid.sort(
            key=lambda item: (
                0 if str(item[0].get("recognition_source", "")) == source_name else 1,
                -float(item[0].get("score", 0.0)),
                int(item[0].get("id", 0)),
            )
        )
        best_span, best_value = valid[0]
        best_score = float(best_span.get("score", 0.0))
        alternatives = [
            {
                "value": value,
                "span_ids": [int(span["id"])],
                "score": float(span.get("score", 0.0)),
                "relation": "fixed_roi",
            }
            for span, value in valid[1:]
            if value != best_value
            and abs(float(span.get("score", 0.0)) - best_score) < 0.03
        ]
        return FieldEvidence(
            field_key=field_key,
            value=best_value,
            span_ids=[int(best_span["id"])],
            score=best_score,
            relation="fixed_roi",
            label="",
            alternatives=alternatives,
        )


def _spans_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    document = payload.get("document", payload)
    blocks = document.get("blocks", []) if isinstance(document, dict) else []
    result = []
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        item = dict(block)
        item["id"] = int(item.get("id", index + 1))
        item["line_id"] = int(item.get("line_id", index + 1))
        item["score"] = float(item.get("score", 0.0))
        item["normalized_box"] = item.get("normalized_box") or item.get("box") or [0, 0, 0, 0]
        result.append(item)
    return result


def _relation(label: dict[str, Any], value: dict[str, Any]) -> tuple[str, float]:
    left = _box(label)
    right = _box(value)
    label_height = max(1.0, left[3] - left[1])
    value_height = max(1.0, right[3] - right[1])
    overlap = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    vertical_overlap = overlap / min(label_height, value_height)
    right_tolerance = min(label_height, value_height)
    if (
        label.get("line_id") == value.get("line_id")
        and right[0] >= left[2] - right_tolerance
        and vertical_overlap >= 0.35
    ):
        return "same_line_right", max(0.0, right[0] - left[2]) / 1000.0
    x_overlap = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    if right[1] >= left[3] and x_overlap / max(1.0, min(left[2] - left[0], right[2] - right[0])) >= 0.25:
        return "next_line_same_column", (right[1] - left[3]) / 1000.0
    lc = ((left[0] + left[2]) / 2, (left[1] + left[3]) / 2)
    rc = ((right[0] + right[2]) / 2, (right[1] + right[3]) / 2)
    return "nearest", hypot(lc[0] - rc[0], lc[1] - rc[1]) / 1000.0


def _validate_value(value: str, definition: dict[str, Any]) -> str:
    text = value.strip()
    if not text:
        return ""
    char_type = str(definition.get("char_type", "any"))
    if char_type == "digits" and not text.isdigit():
        return ""
    if char_type == "alnum" and not all(char.isascii() and char.isalnum() for char in text):
        return ""
    lengths = definition.get("lengths")
    if lengths and len(text) not in {int(length) for length in lengths}:
        return ""
    minimum = int(definition.get("min_length", 0))
    maximum = int(definition.get("max_length", 10000))
    if not minimum <= len(text) <= maximum:
        return ""
    pattern = str(definition.get("regex", "")).strip()
    if pattern:
        match = re.fullmatch(pattern, text)
        if match is None:
            return ""
        if match.lastindex:
            text = str(match.group(1) or "").strip()
            if not text:
                return ""
    return text


def _match_fixed_label(text: str, labels: list[str]) -> str:
    """Match a label only at the start and on a token boundary.

    This prevents a configured label such as ``姓名`` from matching
    ``姓名拼音`` while still accepting ``姓名：张三`` and a label-only span.
    """
    for label in sorted(labels, key=len, reverse=True):
        if text == label:
            return label
        if not text.startswith(label):
            continue
        suffix = text[len(label):]
        if suffix and (suffix[0].isspace() or suffix[0] in ":：=-"):
            return label
    return ""


def _inside_roi(span: dict[str, Any], roi: Any) -> bool:
    if not roi:
        return True
    if not isinstance(roi, (list, tuple)) or len(roi) != 4:
        raise ValidationError("ROI must contain four normalized coordinates")
    box = _box(span)
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return float(roi[0]) <= cx <= float(roi[2]) and float(roi[1]) <= cy <= float(roi[3])


def _strip_roi_label(value: str, labels: list[str]) -> str:
    text = " ".join(value.split()).strip()
    for label in sorted(labels, key=len, reverse=True):
        if label not in text:
            continue
        text = text.split(label, 1)[1].lstrip(" :：=-")
        break
    return text


def _box(span: dict[str, Any]) -> list[float]:
    value = span.get("normalized_box", [0, 0, 0, 0])
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return [0.0, 0.0, 0.0, 0.0]
    return [float(item) for item in value]
