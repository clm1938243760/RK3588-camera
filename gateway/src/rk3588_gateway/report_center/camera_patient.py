from __future__ import annotations

import re
from typing import Any

from .domain import STANDARD_PATIENT_FIELDS, canonical_patient
from .ocr_fields import RuleFieldResolver


DEFAULT_CAMERA_PATIENT_FIELD_DEFINITIONS = (
    ("patient_name", ("患者姓名", "姓名"), "any"),
    ("patient_id", ("患者ID", "患者编号", "病人ID"), "alnum"),
    ("his_exam_no", ("检查号", "检查单号", "申请号", "申请单号"), "alnum"),
    ("report_no", ("报告号", "报告单号"), "alnum"),
    ("exam_item", ("检查项目", "项目名称", "检查名称"), "any"),
    ("sex", ("性别",), "any"),
    ("age", ("年龄",), "any"),
    ("birthday", ("出生日期", "出生年月", "出生年月日"), "any"),
    ("name_phonetic", ("姓名拼音", "拼音"), "any"),
)


def default_camera_patient_fields() -> list[dict[str, Any]]:
    return [
        {
            "field_key": field_key,
            "target": field_key,
            "enabled": True,
            "required": False,
            "label_aliases": list(aliases),
            "char_type": char_type,
            "lengths": [],
            "min_length": 0,
            "max_length": 10000,
            "min_ocr_score": 0.65,
            "relations": ["same_text", "same_line_right", "next_line_same_column", "nearest"],
            "regex": "",
            "roi": None,
        }
        for field_key, aliases, char_type in DEFAULT_CAMERA_PATIENT_FIELD_DEFINITIONS
    ]


class CameraPatientResolver:
    def __init__(self) -> None:
        self.field_resolver = RuleFieldResolver()

    def resolve(
        self,
        ocr_payload: dict[str, Any],
        field_schema: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not field_schema:
            field_schema = default_camera_patient_fields()
        enabled_schema = [
            definition
            for definition in field_schema
            if isinstance(definition, dict) and bool(definition.get("enabled", True))
        ]
        resolved = self.field_resolver.resolve(ocr_payload, enabled_schema)
        patient = canonical_patient(resolved["patient"])
        evidence = dict(resolved["evidence"])
        _derive_patient_components(patient, evidence)

        ocr_status = str(ocr_payload.get("status", ""))
        if ocr_status == "error":
            status = "error"
        elif not evidence:
            status = "rejected"
        elif (
            resolved["missing_fields"]
            or resolved["conflict_fields"]
        ):
            status = "review_required"
        else:
            status = "accepted"

        record = {field: patient.get(field) for field in STANDARD_PATIENT_FIELDS}
        response = _patient_response(status, record)
        return {
            "status": status,
            "response": response,
            "evidence": evidence,
            "missing_fields": list(resolved["missing_fields"]),
            "conflict_fields": list(resolved["conflict_fields"]),
        }


def _patient_response(status: str, patient: dict[str, Any]) -> dict[str, Any]:
    if status == "accepted":
        return {"code": "SUCCESS", "data": [patient], "msg": "成功", "success": True}
    if status == "review_required":
        return {
            "code": "REVIEW_REQUIRED",
            "data": [patient],
            "msg": "患者信息需要复核",
            "success": False,
        }
    if status == "rejected":
        return {"code": "FAIL", "data": [], "msg": "未识别到患者信息", "success": False}
    return {"code": "ERROR", "data": [], "msg": "患者信息处理失败", "success": False}


def _derive_patient_components(
    patient: dict[str, Any], evidence: dict[str, Any]
) -> None:
    name = str(patient.get("patient_name") or "").strip()
    if name:
        if not patient.get("xing"):
            patient["xing"] = name[:1]
            evidence["xing"] = _derived_evidence("patient_name", patient["xing"], evidence)
        if len(name) > 1 and not patient.get("ming"):
            patient["ming"] = name[1:]
            evidence["ming"] = _derived_evidence("patient_name", patient["ming"], evidence)

    birthday = str(patient.get("birthday") or "").strip()
    match = re.fullmatch(
        r"(\d{4})\s*(?:[-/.年])\s*(\d{1,2})\s*(?:[-/.月])\s*(\d{1,2})\s*(?:日)?",
        birthday,
    )
    if match:
        for field, value in zip(("nian", "yue", "ri"), match.groups()):
            if not patient.get(field):
                patient[field] = value.zfill(2) if field in {"yue", "ri"} else value
                evidence[field] = _derived_evidence("birthday", patient[field], evidence)


def _derived_evidence(
    source_field: str,
    value: Any,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    source = evidence.get(source_field, {})
    return {
        "value": value,
        "span_ids": list(source.get("span_ids", [])),
        "score": float(source.get("score", 0.0)),
        "relation": "derived_from_%s" % source_field,
        "label": "",
        "alternatives": [],
    }
