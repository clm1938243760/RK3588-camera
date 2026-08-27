from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .clients import FieldLinkerProtocol, LocalOpenAIChatClient
from .identifier_models import ClassifiedCandidate, IdentifierCandidate, MODEL_IDENTIFIER_TYPES
from .prompt import ModelResponseError
from .settings import LlmSettings


CLASSIFICATION_SYSTEM_PROMPT = """你负责分类医疗申请单上的号码证据。
输入是程序根据OCR坐标构造的标签和值候选。你只能选择已有candidate_id并给出一个允许的type，
不能输出、改写或纠正号码文字。不要把日期、年龄、金额、手机号、身份证号、医生工号、
床号、二维码或条形码当作医疗业务号码。没有可靠语义的候选不要返回。
只返回严格JSON，不要解释。"""

VERIFICATION_SYSTEM_PROMPT = """你负责复核已经分类的医疗号码候选。
逐项判断标签语义、值以及二者关系是否支持给定type。不能修改type，不能生成号码，
不能增加candidate_id。证据不明确、字段混淆或属于排除项时confirmed必须为false。
只返回严格JSON，不要解释。"""

TYPE_GUIDE: Dict[str, str] = {
    "selected_identifier": "由唯一字符数配置选中的通用目标号码，不推断医疗业务类型",
    "patient_id": "患者主索引、患者ID或病人ID；不是住院号、门诊号、卡号或检查号",
    "inpatient_no": "住院号、住院患者编号；不是床号",
    "outpatient_no": "门诊号、门诊患者编号",
    "visit_no": "就诊号、就诊流水号、诊次号",
    "medical_card_no": "就诊卡号、诊疗卡号、医疗卡号；不是卡类型",
    "exam_request_no": "检查申请号、申请单号、医嘱申请号或检查申请流水号",
    "exam_no": "检查号、检查流水号、检查登记号；不是申请单号",
    "imaging_no": "影像号、影像流水号；不是检查申请号",
    "other_medical_id": "标签明确表示医疗业务号码，但不属于以上类型；需要人工复核",
    "ignore": "不是需要返回的医疗业务身份号码",
}

EXPLICIT_LABEL_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("exam_request_no", ("检查申请号", "申请单号", "医嘱申请号", "处方申请号", "处方/申请号", "申请号")),
    ("inpatient_no", ("住院号", "住院编号")),
    ("outpatient_no", ("门诊号", "门诊编号")),
    ("medical_card_no", ("就诊卡号", "诊疗卡号", "医疗卡号", "卡号")),
    ("visit_no", ("就诊号", "就诊流水号", "诊次号")),
    ("imaging_no", ("影像号", "影像流水号")),
    ("exam_no", ("检查流水号", "检查登记号", "检查号")),
    ("patient_id", ("患者id", "病人id", "患者编号", "病人编号", "病历号")),
)


@dataclass(frozen=True)
class BatchLinkOutcome:
    candidates: Tuple[ClassifiedCandidate, ...]
    classification_ms: float
    verification_ms: float
    classification_response: str
    verification_response: str


def explicit_identifier_type(raw_label: str) -> Optional[str]:
    normalized = re.sub(r"[\s:：#()（）\[\]【】]", "", raw_label or "").lower()
    if normalized == "id":
        return "patient_id"
    for identifier_type, terms in EXPLICIT_LABEL_RULES:
        if any(term in normalized for term in terms):
            return identifier_type
    return None


def _json_object(content: str) -> Dict[str, Any]:
    text = content.strip()
    start = text.find("{")
    if start < 0:
        raise ModelResponseError("model response does not contain JSON")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(text[start : index + 1])
                except json.JSONDecodeError as exc:
                    raise ModelResponseError("model JSON is invalid") from exc
                if not isinstance(value, dict):
                    raise ModelResponseError("model JSON must be an object")
                return value
    raise ModelResponseError("model JSON is incomplete")


def parse_classifications(content: str, candidate_ids: Sequence[int]) -> Dict[int, str]:
    payload = _json_object(content)
    if set(payload) != {"classifications"} or not isinstance(payload["classifications"], list):
        raise ModelResponseError("classification response has an invalid schema")
    allowed = set(candidate_ids)
    result: Dict[int, str] = {}
    for item in payload["classifications"]:
        if not isinstance(item, dict) or set(item) != {"candidate_id", "type"}:
            raise ModelResponseError("classification item has an invalid schema")
        candidate_id = item["candidate_id"]
        identifier_type = item["type"]
        if not isinstance(candidate_id, int) or isinstance(candidate_id, bool) or candidate_id not in allowed:
            raise ModelResponseError("classification selected an unknown candidate")
        if identifier_type not in MODEL_IDENTIFIER_TYPES or identifier_type == "ignore":
            raise ModelResponseError("classification selected an invalid identifier type")
        if candidate_id in result:
            raise ModelResponseError("classification repeats a candidate")
        result[candidate_id] = str(identifier_type)
    return result


def parse_verifications(
    content: str,
    expected: Mapping[int, str],
) -> Dict[int, bool]:
    payload = _json_object(content)
    if set(payload) != {"confirmed_candidate_ids"} or not isinstance(
        payload["confirmed_candidate_ids"], list
    ):
        raise ModelResponseError("verification response has an invalid schema")
    confirmed_ids = set()
    for candidate_id in payload["confirmed_candidate_ids"]:
        if not isinstance(candidate_id, int) or isinstance(candidate_id, bool) or candidate_id not in expected:
            raise ModelResponseError("verification selected an unknown candidate")
        if candidate_id in confirmed_ids:
            raise ModelResponseError("verification repeats a candidate")
        confirmed_ids.add(candidate_id)
    return {candidate_id: candidate_id in confirmed_ids for candidate_id in expected}


class BatchIdentifierLinker:
    def __init__(self, chat_client: Optional[FieldLinkerProtocol] = None) -> None:
        self.chat_client = chat_client or LocalOpenAIChatClient()

    def link(
        self,
        candidates: Sequence[IdentifierCandidate],
        settings: LlmSettings,
        allowed_types_by_id: Optional[Mapping[int, Sequence[str]]] = None,
    ) -> BatchLinkOutcome:
        by_id = {candidate.id: candidate for candidate in candidates}
        explicit_types = {
            candidate.id: identifier_type
            for candidate in candidates
            if (identifier_type := explicit_identifier_type(candidate.raw_label)) is not None
            and (
                allowed_types_by_id is None
                or identifier_type in set(allowed_types_by_id.get(candidate.id, ()))
            )
        }
        classification_prompt = {
            "task": "从候选中返回所有可靠的医疗身份或检查业务号码，并分类",
            "allowed_types": TYPE_GUIDE,
            "rules": [
                "通常只返回有明确字段标签支撑的候选；带allowed_rule_types的候选可在这些类型中辅助判断。",
                "严格区分患者ID、住院号、门诊号、就诊号、就诊卡号、检查申请号和检查号。",
                "候选带explicit_label_type时必须返回该candidate_id并使用该type。",
                "other_medical_id只用于标签明确但确实无法归入核心类型的医疗号码。",
                "省略应当ignore的候选，不要在输出中返回ignore。",
            ],
            "required_output": {"classifications": [{"candidate_id": 1, "type": "patient_id"}]},
            "candidates": [
                {
                    **candidate.to_prompt_dict(),
                    **(
                        {"explicit_label_type": explicit_types[candidate.id]}
                        if candidate.id in explicit_types
                        else {}
                    ),
                    **(
                        {"allowed_rule_types": list(allowed_types_by_id[candidate.id])}
                        if allowed_types_by_id is not None and candidate.id in allowed_types_by_id
                        else {}
                    ),
                }
                for candidate in candidates
            ],
        }
        started = time.monotonic()
        classification_response = self._request_with_retry(
            CLASSIFICATION_SYSTEM_PROMPT,
            classification_prompt,
            settings,
            lambda value: self._parse_constrained_classifications(
                value, list(by_id), allowed_types_by_id
            ),
        )
        classification_ms = (time.monotonic() - started) * 1000.0
        classifications = self._parse_constrained_classifications(
            classification_response, list(by_id), allowed_types_by_id
        )
        classifications.update(explicit_types)
        if not classifications:
            return BatchLinkOutcome((), classification_ms, 0.0, classification_response, "")

        verification_prompt = {
            "task": "从classified_candidates中选择所有通过复核的candidate_id",
            "type_guide": TYPE_GUIDE,
            "rules": [
                "输出必须且只能是一个JSON对象。",
                "JSON只能有confirmed_candidate_ids一个键。",
                "该键的值只能是整数数组。",
                "不解释，不返回verified、reason、revisions或其他键。",
            ],
            "required_output": {"confirmed_candidate_ids": [1]},
            "classified_candidates": [
                {
                    **by_id[candidate_id].to_prompt_dict(),
                    "type": identifier_type,
                }
                for candidate_id, identifier_type in classifications.items()
            ],
        }
        started = time.monotonic()
        verification_response = self._request_with_retry(
            VERIFICATION_SYSTEM_PROMPT,
            verification_prompt,
            settings,
            lambda value: parse_verifications(value, classifications),
        )
        verification_ms = (time.monotonic() - started) * 1000.0
        verified = parse_verifications(verification_response, classifications)
        linked = tuple(
            ClassifiedCandidate(
                candidate=by_id[candidate_id],
                identifier_type=identifier_type,
                confirmed=verified[candidate_id],
                reasons=() if verified[candidate_id] else ("model_verification_rejected",),
                decision_source="model",
            )
            for candidate_id, identifier_type in classifications.items()
        )
        return BatchLinkOutcome(
            linked,
            classification_ms,
            verification_ms,
            classification_response,
            verification_response,
        )

    @staticmethod
    def _parse_constrained_classifications(content, candidate_ids, allowed_types_by_id):
        classifications = parse_classifications(content, candidate_ids)
        if allowed_types_by_id is None:
            return classifications
        for candidate_id, identifier_type in classifications.items():
            if identifier_type not in set(allowed_types_by_id.get(candidate_id, ())):
                raise ModelResponseError("classification selected a type outside configured rules")
        return classifications

    def _request_with_retry(self, system_prompt, payload, settings, parser):
        prompt = dict(payload)
        last_error: Optional[ModelResponseError] = None
        for attempt in range(2):
            response = self.chat_client.link(
                system_prompt,
                json.dumps(prompt, ensure_ascii=False, separators=(",", ":")),
                settings,
            )
            try:
                parser(response)
                return response
            except ModelResponseError as exc:
                last_error = exc
                if attempt == 0:
                    prompt["correction"] = "上一次输出不符合JSON协议：%s。只返回required_output结构。" % exc
                    prompt["previous_invalid_output"] = response[:1000]
        raise last_error or ModelResponseError("model response is invalid")
