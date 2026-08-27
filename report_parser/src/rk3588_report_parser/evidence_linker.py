from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .choice_linker import ConstrainedChoiceLinker
from .clients import LocalSpanChoiceClient, SpanChoiceClientProtocol
from .models import FIELD_NAMES
from .prompt import FIELD_GUIDE, ModelResponseError
from .settings import LlmSettings
from .validation import is_plausible_field_value


EVIDENCE_LABEL_SYSTEM_PROMPT = """你负责从医疗报告 OCR 文本中寻找字段标签证据。
根据字段含义选择一个明确表示该字段的标签 span ID。不同医院的标签文字可以不同，
必须按语义判断，不能按固定关键词匹配。类似 \"ID:60017768119\" 的标签和值合并
文本可以作为 patient_id 的标签证据。不要把裸值、其他字段或仅仅格式相似的内容
当成标签；没有可靠标签时选择 0。只返回受约束的十进制 ID。"""

EVIDENCE_VALUE_SYSTEM_PROMPT = """你负责把已选字段标签与它支配的 OCR 值证据关联起来。
从候选项中选择语义上属于该标签的值。候选项可能是单个 span、标签和值合并的
span，或者连续多行值。没有可靠关系时选择 0。只返回受约束的十进制候选 ID，
绝不编造文字。"""


# These describe field-label semantics for the model. They are not OCR keyword
# matches: no program branch compares report text with these phrases.
LABEL_GUIDE = {
    "patient_name": "表示患者本人姓名的字段标签，不是姓名值，也不是卡类型或编号字段",
    "patient_id": "明确表示患者ID或病人ID的字段标签；排除卡类型、就诊卡号、检查号、申请号和报告号",
    "sex": "表示患者性别的字段标签",
    "age": "表示患者年龄的字段标签",
    "birthday": "表示患者出生日期的字段标签，不是报告日期或检查日期",
    "his_exam_no": "表示 HIS 检查号、申请单号、处方申请号或流水号的字段标签",
    "report_no": "表示当前报告自身编号的字段标签，不是患者 ID 或申请单号",
    "report_date": "表示报告日期、检查日期或出具日期的字段标签，不是出生日期",
    "exam_item": "表示检查项目、检查名称或检查程序内容的字段标签或表格列标题",
}


# Reserve the patient's primary identifier before less distinctive free-text
# fields. This affects only model-query order and contains no layout knowledge.
EVIDENCE_SELECTION_ORDER = (
    "patient_id",
    "patient_name",
    "sex",
    "age",
    "birthday",
    "report_no",
    "report_date",
    "exam_item",
    "his_exam_no",
)


@dataclass(frozen=True)
class ValueOption:
    option_id: int
    value_span_ids: Tuple[int, ...]
    value_mode: str
    value_text: str

    def to_prompt_dict(self) -> Dict[str, Any]:
        return {
            "id": self.option_id,
            "value_span_ids": list(self.value_span_ids),
            "value_mode": self.value_mode,
            "text": self.value_text,
        }


def _split_after_delimiter(text: str) -> Optional[str]:
    match = re.search(r"[:\uFF1A]", text)
    if match is None:
        return None
    label = text[: match.start()].strip()
    value = text[match.end() :].strip()
    return value if label and value else None


def _span_line(span: Dict[str, Any]) -> int:
    value = span.get("line")
    return int(value) if isinstance(value, (int, float)) else 0


def _span_box(span: Dict[str, Any]) -> Tuple[int, int, int, int]:
    value = span.get("box")
    if not isinstance(value, list) or len(value) != 4:
        return (0, 0, 0, 0)
    return tuple(int(item) for item in value)  # type: ignore[return-value]


def _join_text(span_ids: Sequence[int], by_id: Dict[int, Dict[str, Any]]) -> str:
    return "".join(str(by_id[span_id]["text"]) for span_id in span_ids).strip()


def _is_after(label: Dict[str, Any], candidate: Dict[str, Any]) -> bool:
    label_line, candidate_line = _span_line(label), _span_line(candidate)
    if candidate_line < label_line:
        return False
    label_box, candidate_box = _span_box(label), _span_box(candidate)
    if candidate_line == label_line:
        return candidate_box[0] >= label_box[2] - 20
    return candidate_line - label_line <= 2


def _contiguous_following(
    start: Dict[str, Any],
    spans: Sequence[Dict[str, Any]],
    used_ids: Sequence[int],
    maximum: int,
) -> List[Dict[str, Any]]:
    blocked = set(used_ids)
    ordered = sorted(spans, key=lambda item: (_span_line(item), _span_box(item)[0], item["id"]))
    try:
        start_index = next(index for index, item in enumerate(ordered) if item["id"] == start["id"])
    except StopIteration:
        return [start]

    selected = [start]
    previous = start
    for item in ordered[start_index + 1 :]:
        if item["id"] in blocked:
            continue
        line_gap = _span_line(item) - _span_line(previous)
        if line_gap < 0:
            continue
        if line_gap > 1:
            break
        selected.append(item)
        previous = item
        if len(selected) >= maximum:
            break
    return selected


class EvidenceChoiceLinker:
    """Desktop-only model linker that selects label and value OCR evidence."""

    def __init__(
        self,
        choice_client: Optional[SpanChoiceClientProtocol] = None,
        max_candidate_spans: int = 127,
        max_exam_item_spans: int = 8,
        target_fields: Optional[Sequence[str]] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        if max_candidate_spans < 1 or max_candidate_spans > 127:
            raise ValueError("max_candidate_spans must be from 1 to 127")
        if max_exam_item_spans < 1 or max_exam_item_spans > 16:
            raise ValueError("max_exam_item_spans must be from 1 to 16")
        self.choice_client = choice_client or LocalSpanChoiceClient()
        self.max_candidate_spans = max_candidate_spans
        self.max_exam_item_spans = max_exam_item_spans
        requested = tuple(target_fields) if target_fields is not None else EVIDENCE_SELECTION_ORDER
        unknown = set(requested) - set(FIELD_NAMES)
        if unknown or not requested:
            raise ValueError("target_fields must contain supported fields")
        self.selection_order = tuple(field for field in EVIDENCE_SELECTION_ORDER if field in requested)
        self.progress_callback = progress_callback

    def link(self, system_prompt: str, user_prompt: str, settings: LlmSettings) -> str:
        del system_prompt
        payload = ConstrainedChoiceLinker._prompt_payload(user_prompt)
        spans = ConstrainedChoiceLinker._normalize_spans(payload)
        if len(spans) > self.max_candidate_spans:
            raise ModelResponseError(
                "evidence choice has %d OCR spans; maximum is %d"
                % (len(spans), self.max_candidate_spans)
            )

        associations = {
            field: {"label_span_ids": [], "value_span_ids": [], "value_mode": "full_span"}
            for field in FIELD_NAMES
        }
        used_ids: List[int] = []
        total_fields = len(self.selection_order)
        for index, field in enumerate(self.selection_order, start=1):
            label_candidates = [span for span in spans if span["id"] not in used_ids]
            if not label_candidates:
                continue
            self._progress("evidence %d/%d: %s choose label" % (index, total_fields, field))
            label_prompt = {
                "stage": "label",
                "field": field,
                "label_meaning": LABEL_GUIDE[field],
                "value_meaning": FIELD_GUIDE[field],
                "rules": [
                    "选择字段标题或键，不要选择裸值。",
                    "标签和值合并的 span 只有在标签部分确实表示当前字段时才可选择。",
                    "不要因为值的格式相似就选择另一个字段。",
                    "不同医院用词可以不同，按语义判断，不使用固定关键词表。",
                    "没有标签或存在歧义时选择 0。",
                ],
                "ocr_context": spans,
                "selectable_candidates": [{"id": 0, "text": "NO_EXPLICIT_LABEL"}] + label_candidates,
            }
            label_ids = [0] + [int(span["id"]) for span in label_candidates]
            label_id = self.choice_client.select(
                EVIDENCE_LABEL_SYSTEM_PROMPT,
                json.dumps(label_prompt, ensure_ascii=False, separators=(",", ":")),
                settings,
                label_ids,
            )
            if label_id == 0:
                self._progress("evidence %d/%d: %s has no reliable label" % (index, total_fields, field))
                continue

            label = next(span for span in spans if span["id"] == label_id)
            options = self._value_options(field, label, spans, used_ids)
            if not options:
                self._progress("evidence %d/%d: %s label has no valid value option" % (index, total_fields, field))
                continue

            self._progress(
                "evidence %d/%d: %s choose value from %d options"
                % (index, total_fields, field, len(options))
            )
            value_prompt = {
                "stage": "value",
                "field": field,
                "field_meaning": FIELD_GUIDE[field],
                "selected_label": label,
                "rules": [
                    "只能选择由 selected_label 支配的值。",
                    "标签和值位于同一 span 时，可以选择 after_delimiter 候选项。",
                    "关系不明确时选择 0。",
                ],
                "ocr_context": spans,
                "selectable_options": [
                    {"id": 0, "value_span_ids": [], "text": "NO_RELIABLE_VALUE"}
                ] + [option.to_prompt_dict() for option in options],
            }
            option_ids = [0] + [option.option_id for option in options]
            choice_id = self.choice_client.select(
                EVIDENCE_VALUE_SYSTEM_PROMPT,
                json.dumps(value_prompt, ensure_ascii=False, separators=(",", ":")),
                settings,
                option_ids,
            )
            if choice_id == 0:
                continue
            chosen = next(option for option in options if option.option_id == choice_id)
            associations[field] = {
                "label_span_ids": [label_id],
                "value_span_ids": list(chosen.value_span_ids),
                "value_mode": chosen.value_mode,
            }
            for span_id in (label_id, *chosen.value_span_ids):
                if span_id not in used_ids:
                    used_ids.append(span_id)

        return json.dumps(associations, ensure_ascii=False, separators=(",", ":"))

    def _value_options(
        self,
        field: str,
        label: Dict[str, Any],
        spans: Sequence[Dict[str, Any]],
        used_ids: Sequence[int],
    ) -> List[ValueOption]:
        by_id = {int(span["id"]): span for span in spans}
        raw_options: List[Tuple[Tuple[int, ...], str, str]] = []

        delimited_value = _split_after_delimiter(str(label["text"]))
        if delimited_value and is_plausible_field_value(field, delimited_value):
            raw_options.append(((int(label["id"]),), "after_delimiter", delimited_value))

        nearby = [
            span
            for span in spans
            if span["id"] != label["id"]
            and span["id"] not in used_ids
            and _is_after(label, span)
        ]
        nearby.sort(
            key=lambda item: (
                _span_line(item) - _span_line(label),
                abs(_span_box(item)[0] - _span_box(label)[0]),
                item["id"],
            )
        )
        for candidate in nearby:
            candidate_id = int(candidate["id"])
            candidate_text = str(candidate["text"])
            # A second combined key:value span is evidence for its own key, not
            # a free-standing value governed by the previously selected label.
            if _split_after_delimiter(candidate_text) is not None:
                continue
            if is_plausible_field_value(field, candidate_text):
                raw_options.append(((candidate_id,), "full_span", candidate_text))
            if field != "exam_item":
                continue
            continuation = _contiguous_following(
                candidate,
                spans,
                used_ids,
                self.max_exam_item_spans,
            )
            for length in range(2, len(continuation) + 1):
                span_ids = tuple(int(item["id"]) for item in continuation[:length])
                value_text = _join_text(span_ids, by_id)
                if is_plausible_field_value(field, value_text):
                    raw_options.append((span_ids, "full_span", value_text))

        options: List[ValueOption] = []
        seen = set()
        for span_ids, mode, value_text in raw_options:
            key = (span_ids, mode)
            if key in seen:
                continue
            seen.add(key)
            options.append(
                ValueOption(
                    option_id=len(options) + 1,
                    value_span_ids=span_ids,
                    value_mode=mode,
                    value_text=value_text,
                )
            )
            if len(options) >= 127:
                break
        return options

    def _progress(self, message: str) -> None:
        if self.progress_callback is not None:
            self.progress_callback(message)
