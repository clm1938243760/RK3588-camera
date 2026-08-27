from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Sequence

from .choice_linker import ConstrainedChoiceLinker
from .clients import FieldLinkerProtocol, LocalOpenAIChatClient
from .evidence_linker import EVIDENCE_SELECTION_ORDER, LABEL_GUIDE, EvidenceChoiceLinker
from .models import FIELD_NAMES
from .prompt import FIELD_GUIDE, ModelResponseError
from .settings import LlmSettings


EVIDENCE_CHAT_SYSTEM_PROMPT = """你负责把医疗报告 OCR 证据关联到一个指定字段。
你不能查看原图、不能编造文字、不能返回 OCR 中不存在的 ID。先根据语义选择字段标签，
再选择由该标签支配的值。不同医院的标签文字和版式可以不同，不使用固定关键词或坐标模板。
只返回要求的 JSON 对象，不输出解释或 Markdown。"""


def _json_object(content: str) -> Dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        text = text[first_newline + 1 :] if first_newline >= 0 else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    try:
        parsed = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise ModelResponseError("evidence chat response is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ModelResponseError("evidence chat response must be an object")
    return parsed


def _span_ids(value: Any, name: str, known_ids: Sequence[int], maximum: int) -> List[int]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ModelResponseError("evidence chat %s must be a short array" % name)
    normalized: List[int] = []
    for item in value:
        if not isinstance(item, int) or isinstance(item, bool) or item not in known_ids:
            raise ModelResponseError("evidence chat %s contains an unknown span" % name)
        if item in normalized:
            raise ModelResponseError("evidence chat %s repeats a span" % name)
        normalized.append(item)
    return normalized


def parse_single_field_evidence(content: str, known_ids: Sequence[int]) -> Dict[str, Any]:
    parsed = _json_object(content)
    expected = {"label_span_ids", "value_span_ids", "value_mode"}
    if set(parsed) != expected:
        raise ModelResponseError("evidence chat response has an invalid schema")
    labels = _span_ids(parsed["label_span_ids"], "label_span_ids", known_ids, 1)
    values = _span_ids(parsed["value_span_ids"], "value_span_ids", known_ids, 8)
    mode = parsed["value_mode"]
    if mode not in {"full_span", "after_delimiter"}:
        raise ModelResponseError("evidence chat value_mode is invalid")
    if not labels and (values or mode != "full_span"):
        raise ModelResponseError("evidence chat returned a value without a label")
    if mode == "after_delimiter" and (len(labels) != 1 or values != labels):
        raise ModelResponseError("after_delimiter must use one shared label/value span")
    return {
        "label_span_ids": labels,
        "value_span_ids": values,
        "value_mode": mode,
    }


def parse_option_choice(content: str, allowed_ids: Sequence[int]) -> int:
    parsed = _json_object(content)
    if set(parsed) != {"option_id"}:
        raise ModelResponseError("evidence chat option response has an invalid schema")
    option_id = parsed["option_id"]
    if not isinstance(option_id, int) or isinstance(option_id, bool) or option_id not in allowed_ids:
        raise ModelResponseError("evidence chat selected an unknown option")
    return option_id


def parse_confirmation(content: str) -> bool:
    parsed = _json_object(content)
    if set(parsed) != {"confirmed"} or not isinstance(parsed["confirmed"], bool):
        raise ModelResponseError("evidence confirmation has an invalid schema")
    return bool(parsed["confirmed"])


class EvidenceChatLinker:
    """Use normal local chat generation to select label and value span IDs."""

    def __init__(
        self,
        chat_client: Optional[FieldLinkerProtocol] = None,
        target_fields: Optional[Sequence[str]] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.chat_client = chat_client or LocalOpenAIChatClient()
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
        associations = {
            field: {"label_span_ids": [], "value_span_ids": [], "value_mode": "full_span"}
            for field in FIELD_NAMES
        }
        used_ids: List[int] = []
        total = len(self.selection_order)

        for index, field in enumerate(self.selection_order, start=1):
            self._progress("evidence chat %d/%d: %s" % (index, total, field))
            evidence_options = self._evidence_options(field, spans, used_ids)
            if not evidence_options:
                continue
            field_prompt = {
                "task": "从候选证据关系中选择最符合当前字段语义的一项",
                "field": field,
                "label_meaning": LABEL_GUIDE[field],
                "value_meaning": FIELD_GUIDE[field],
                "rules": [
                    "候选项已经通过格式和位置检查，你只负责判断标签和值的语义是否属于当前字段。",
                    "不要把卡类型、卡号、申请号、检查号、报告号和患者主 ID 相互混淆。",
                    "不要把字段值或表格数据行当成字段标签。",
                    "没有可靠候选时选择 option_id 0。",
                ],
                "required_output": {"option_id": 0},
                "candidate_evidence_options": evidence_options,
                "already_used_span_ids": used_ids,
            }
            selected_option_id: Optional[int] = None
            last_error: Optional[ModelResponseError] = None
            for attempt in range(2):
                response = self.chat_client.link(
                    EVIDENCE_CHAT_SYSTEM_PROMPT,
                    json.dumps(field_prompt, ensure_ascii=False, separators=(",", ":")),
                    settings,
                )
                try:
                    selected_option_id = parse_option_choice(
                        response,
                        [0] + [int(option["option_id"]) for option in evidence_options],
                    )
                    break
                except ModelResponseError as exc:
                    last_error = exc
                    if attempt == 0:
                        field_prompt["correction"] = (
                            "上一次输出无效：%s。只能返回 {\"option_id\":候选数字}，不要输出其他键。"
                        ) % exc
                        field_prompt["previous_invalid_output"] = response
            if selected_option_id is None:
                raise last_error or ModelResponseError("evidence chat response is invalid")
            if selected_option_id == 0:
                continue
            chosen = next(
                option for option in evidence_options if option["option_id"] == selected_option_id
            )
            if not self._confirm(field, chosen, settings):
                continue
            selected = {
                "label_span_ids": list(chosen["label_span_ids"]),
                "value_span_ids": list(chosen["value_span_ids"]),
                "value_mode": chosen["value_mode"],
            }
            associations[field] = selected
            for span_id in selected["label_span_ids"] + selected["value_span_ids"]:
                if span_id not in used_ids:
                    used_ids.append(span_id)

        return json.dumps(associations, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _evidence_options(
        field: str,
        spans: Sequence[Dict[str, Any]],
        used_ids: Sequence[int],
    ) -> List[Dict[str, Any]]:
        builder = EvidenceChoiceLinker()
        blocked = set(used_ids)
        options: List[Dict[str, Any]] = []
        for label in spans:
            label_id = int(label["id"])
            if label_id in blocked:
                continue
            value_options = builder._value_options(field, label, spans, used_ids)
            if field == "exam_item" and value_options:
                delimited = [value for value in value_options if value.value_mode == "after_delimiter"]
                full = [value for value in value_options if value.value_mode == "full_span"]
                if full:
                    first_value_id = full[0].value_span_ids[0]
                    group = [value for value in full if value.value_span_ids[0] == first_value_id]
                    indexes = {0, len(group) // 3, (2 * len(group)) // 3, len(group) - 1}
                    full = [group[index] for index in sorted(indexes)]
                value_options = delimited[:1] + full
            else:
                value_options = value_options[:4]
            for value in value_options:
                if any(span_id in blocked for span_id in value.value_span_ids):
                    continue
                options.append(
                    {
                        "option_id": len(options) + 1,
                        "label_span_ids": [label_id],
                        "label_text": str(label["text"]),
                        "value_span_ids": list(value.value_span_ids),
                        "value_text": value.value_text,
                        "value_mode": value.value_mode,
                    }
                )
                if len(options) >= 96:
                    return options
        return options

    def _confirm(self, field: str, chosen: Dict[str, Any], settings: LlmSettings) -> bool:
        prompt = {
            "task": "复核所选标签和值是否明确属于指定字段",
            "field": field,
            "label_meaning": LABEL_GUIDE[field],
            "value_meaning": FIELD_GUIDE[field],
            "selected_evidence": chosen,
            "rules": [
                "只有标签文本明确表示当前字段，并且值确实由该标签支配时才返回 true。",
                "仅仅值格式合法不够；卡号、患者ID、检查申请号、报告号必须严格区分。",
                "字段标签不明确、标签其实是数据值、或多行范围混入其他字段时返回 false。",
            ],
            "required_output_schema": {
                "type": "object",
                "properties": {"confirmed": {"type": "boolean"}},
                "required": ["confirmed"],
                "additionalProperties": False,
            },
        }
        response = self.chat_client.link(
            EVIDENCE_CHAT_SYSTEM_PROMPT,
            json.dumps(prompt, ensure_ascii=False, separators=(",", ":")),
            settings,
        )
        try:
            return parse_confirmation(response)
        except ModelResponseError:
            return False

    def _progress(self, message: str) -> None:
        if self.progress_callback is not None:
            self.progress_callback(message)
