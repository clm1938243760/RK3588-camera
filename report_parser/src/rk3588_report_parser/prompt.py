from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence

from .models import FIELD_NAMES, OcrSpan


SYSTEM_PROMPT = """你是医疗报告 OCR 证据关联器。你不能识别图片，也不能编造任何文字。
你的唯一任务是：根据 OCR 片段的文字、阅读行和归一化坐标，把每个字段关联到最可信的值片段 ID。
不要把“姓名”“患者ID”“性别”等字段标题当作值。没有高置信值时使用空数组。
每个 span ID 只能用于一个非空字段。输出只能是 JSON 对象，不能输出 Markdown、解释、字段值或额外字段。"""

FIELD_GUIDE = {
    "patient_name": "患者姓名，不是姓名标题",
    "patient_id": "患者ID、病人ID、门诊号、住院号等患者主标识，仅选实际编号",
    "sex": "患者性别，仅选男、女、男性、女性等实际值",
    "age": "患者年龄，仅选数值或数值加岁",
    "birthday": "患者出生日期",
    "his_exam_no": "HIS检查号、检查流水号、申请单号等检查标识",
    "report_no": "报告号、报告编号等报告标识",
    "report_date": "报告日期、检查日期、出具日期等日期",
    "exam_item": "检查项目、检查名称、检验项目的实际内容",
}


OUTPUT_CONTRACT = """
Strict output contract:
- Return one JSON object and no prose or Markdown.
- It must contain exactly these nine keys: patient_name, patient_id, sex, age,
  birthday, his_exam_no, report_no, report_date, exam_item.
- Each key maps directly to an array of OCR span IDs, for example
  {"patient_name":[2],"patient_id":[4],"sex":[],"age":[],"birthday":[],
   "his_exam_no":[],"report_no":[],"report_date":[],"exam_item":[]}.
- Array items are decimal span IDs only. Do not quote IDs. Do not return OCR
  text, labels, values, confidence, coordinates, span_ids, or any other keys.
- A nearby label and its value are different spans. Select the value span, not
  the label span. A two-to-four Chinese-character personal name beside 姓名 is
  a patient_name value.
- adjacent_same_line_pairs are geometry evidence. If a field label is the left
  ID and a value is its adjacent right ID, prefer the right ID for that field.
- fixed_links are immutable program-selected OCR evidence. Keep those span IDs
  unchanged for their fields and leave a field empty only when fixed_links is empty.
"""
SYSTEM_PROMPT += "\n\n" + OUTPUT_CONTRACT


class ModelResponseError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedFieldAssociations:
    value_links: Dict[str, List[int]]
    label_links: Dict[str, List[int]]
    value_modes: Dict[str, str]


def _adjacent_same_line_pairs(spans: Sequence[OcrSpan]) -> List[List[int]]:
    by_line: Dict[int, List[OcrSpan]] = {}
    for span in spans:
        by_line.setdefault(span.line_id, []).append(span)
    pairs: List[List[int]] = []
    for line_id in sorted(by_line):
        line = sorted(by_line[line_id], key=lambda span: (span.box[0], span.id))
        for left, right in zip(line, line[1:]):
            pairs.append([left.id, right.id])
    return pairs


def build_user_prompt(
    spans: Sequence[OcrSpan],
    fixed_links: Optional[Mapping[str, Sequence[int]]] = None,
) -> str:
    fixed = {
        field: list(fixed_links.get(field, [])) if fixed_links is not None else []
        for field in FIELD_NAMES
    }
    schema = {field: [] for field in FIELD_NAMES}
    payload = {
        "task": "从下面 OCR spans 中选择字段值的 span_ids",
        "fields": FIELD_GUIDE,
        "required_output_schema": schema,
        "format_example": {
            "input_hint": "If span 1 is 姓名 and span 2 is a patient name, select 2.",
            "output": {
                "patient_name": [2],
                "patient_id": [],
                "sex": [],
                "age": [],
                "birthday": [],
                "his_exam_no": [],
                "report_no": [],
                "report_date": [],
                "exam_item": [],
            },
        },
        "adjacent_same_line_pairs": _adjacent_same_line_pairs(spans),
        "fixed_links": fixed,
        "spans": [span.to_prompt_dict() for span in spans],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _extract_json_object(content: str) -> Dict[str, object]:
    text = content.strip()
    if text.startswith("```"):
        parts = text.split("\n", 1)
        text = parts[1] if len(parts) == 2 else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]

    start = text.find("{")
    if start < 0:
        raise ModelResponseError("model response does not contain JSON")
    depth = 0
    in_string = False
    escaped = False
    end = -1
    for index in range(start, len(text)):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if end < 0:
        raise ModelResponseError("model JSON is incomplete")
    try:
        parsed = json.loads(text[start:end])
    except json.JSONDecodeError as exc:
        raise ModelResponseError("model JSON is invalid") from exc
    if not isinstance(parsed, dict):
        raise ModelResponseError("model JSON must be an object")
    return parsed


def _normalize_span_ids(value: object, field: str, name: str) -> List[int]:
    if not isinstance(value, list):
        raise ModelResponseError("model field %s has invalid %s" % (field, name))
    normalized: List[int] = []
    for item in value:
        if isinstance(item, int) and not isinstance(item, bool):
            normalized.append(item)
        elif isinstance(item, str) and item.isascii() and item.isdigit() and str(int(item)) == item:
            normalized.append(int(item))
        else:
            raise ModelResponseError("model field %s has invalid %s" % (field, name))
    if len(normalized) != len(set(normalized)):
        raise ModelResponseError("model field %s repeats a span ID in %s" % (field, name))
    return normalized


def parse_field_associations(content: str) -> ParsedFieldAssociations:
    raw = _extract_json_object(content)
    unexpected = set(raw) - set(FIELD_NAMES)
    missing = set(FIELD_NAMES) - set(raw)
    if unexpected or missing:
        details = []
        if unexpected:
            details.append("unexpected=" + ",".join(sorted(unexpected)))
        if missing:
            details.append("missing=" + ",".join(sorted(missing)))
        raise ModelResponseError("model schema mismatch: " + " ".join(details))

    value_links: Dict[str, List[int]] = {}
    label_links: Dict[str, List[int]] = {}
    value_modes: Dict[str, str] = {}
    for field in FIELD_NAMES:
        item = raw[field]
        if isinstance(item, dict):
            keys = set(item)
            if keys == {"span_ids"}:
                value_links[field] = _normalize_span_ids(item["span_ids"], field, "span_ids")
                label_links[field] = []
                value_modes[field] = "full_span"
                continue
            expected = {"label_span_ids", "value_span_ids", "value_mode"}
            if keys != expected:
                raise ModelResponseError("model field %s has invalid evidence schema" % field)
            labels = _normalize_span_ids(item["label_span_ids"], field, "label_span_ids")
            values = _normalize_span_ids(item["value_span_ids"], field, "value_span_ids")
            mode = item["value_mode"]
            if mode not in {"full_span", "after_delimiter"}:
                raise ModelResponseError("model field %s has invalid value_mode" % field)
            if len(labels) > 1:
                raise ModelResponseError("model field %s has too many label spans" % field)
            label_links[field] = labels
            value_links[field] = values
            value_modes[field] = str(mode)
        else:
            value_links[field] = _normalize_span_ids(item, field, "span_ids")
            label_links[field] = []
            value_modes[field] = "full_span"

    return ParsedFieldAssociations(
        value_links=value_links,
        label_links=label_links,
        value_modes=value_modes,
    )


def parse_field_links(content: str) -> Dict[str, List[int]]:
    return parse_field_associations(content).value_links
