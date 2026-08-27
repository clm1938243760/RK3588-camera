from __future__ import annotations

import datetime as dt
import re
from dataclasses import replace
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .evidence import extract_after_delimiter
from .models import FIELD_NAMES, IDENTIFIER_FIELDS, FieldEvidence, OcrSpan, empty_fields
from .settings import ValidationSettings


LABEL_TEXTS = {
    "姓名",
    "患者姓名",
    "病人姓名",
    "性别",
    "年龄",
    "出生日期",
    "生日",
    "患者ID",
    "病人ID",
    "门诊号",
    "住院号",
    "检查号",
    "检查流水号",
    "申请单号",
    "报告号",
    "报告编号",
    "报告日期",
    "检查日期",
    "检查项目",
    "检查名称",
    "检验项目",
}
FIELD_LABEL_PREFIXES = {
    "patient_name": ("患者姓名", "病人姓名", "姓名"),
    "patient_id": ("患者编号", "患者ID", "病人ID", "门诊号", "住院号", "患者号"),
    "sex": ("性别",),
    "age": ("年龄",),
    "birthday": ("出生年月日", "出生日期", "生日"),
    "his_exam_no": ("HIS检查号", "检查流水号", "申请单号", "检查号"),
    "report_no": ("报告编号", "报告号"),
    "report_date": ("报告日期", "检查日期", "出具日期"),
    "exam_item": ("检查项目", "检查名称", "检验项目"),
}
LABEL_TEXTS.update(
    {
        "Name",
        "Patient Name",
        "Patient ID",
        "Sex",
        "Age",
        "Birthday",
        "Date of Birth",
        "HIS Exam No",
        "Exam No",
        "Report No",
        "Report Date",
        "Exam Item",
    }
)
FIELD_LABEL_PREFIXES["patient_name"] += ("Patient Name", "Name")
FIELD_LABEL_PREFIXES["patient_id"] += ("Patient ID",)
FIELD_LABEL_PREFIXES["sex"] += ("Sex",)
FIELD_LABEL_PREFIXES["age"] += ("Age",)
FIELD_LABEL_PREFIXES["birthday"] += ("Date of Birth", "Birthday")
FIELD_LABEL_PREFIXES["his_exam_no"] += ("HIS Exam No", "Exam No")
FIELD_LABEL_PREFIXES["report_no"] += ("Report No",)
FIELD_LABEL_PREFIXES["report_date"] += ("Report Date",)
FIELD_LABEL_PREFIXES["exam_item"] += ("Exam Item",)

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{3,63}$")
NAME_PATTERN = re.compile(r"^[\u3400-\u9fffA-Za-z·]{2,32}$")
AGE_PATTERN = re.compile(r"^(\d{1,3})(?:岁)?$")
DATE_PATTERN = re.compile(r"(\d{4})\s*(?:-|/|年|\.)\s*(\d{1,2})\s*(?:-|/|月|\.)\s*(\d{1,2})(?:日)?")


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def _strip_field_label(value: str, field: str) -> str:
    """Handle an OCR item containing a generic label and its value together."""

    for label in FIELD_LABEL_PREFIXES[field]:
        pattern = r"^\s*" + re.escape(label) + r"\s*(?:[:：#]|-)?\s*"
        if re.match(pattern, value, flags=re.IGNORECASE):
            return re.sub(pattern, "", value, count=1, flags=re.IGNORECASE).strip()
    return value.strip()


def _join_selected(spans: Sequence[OcrSpan]) -> str:
    if not spans:
        return ""
    ordered = sorted(spans, key=lambda span: (span.line_id, span.box[0], span.id))
    return "".join(span.text for span in ordered).strip()


def _validate_name(value: str) -> Tuple[str, List[str]]:
    normalized = _compact(value)
    reasons: List[str] = []
    if normalized in LABEL_TEXTS:
        reasons.append("label_selected_as_value")
    if not NAME_PATTERN.fullmatch(normalized):
        reasons.append("invalid_patient_name")
    return normalized, reasons


def _validate_identifier(value: str, field: str) -> Tuple[str, List[str]]:
    normalized = _compact(value)
    reasons: List[str] = []
    if normalized in LABEL_TEXTS:
        reasons.append("label_selected_as_value")
    if not IDENTIFIER_PATTERN.fullmatch(normalized):
        reasons.append("invalid_%s" % field)
    return normalized, reasons


def _validate_sex(value: str) -> Tuple[str, List[str]]:
    normalized = _compact(value).lower()
    mapping = {"男": "男", "男性": "男", "m": "男", "male": "男", "女": "女", "女性": "女", "f": "女", "female": "女"}
    if normalized not in mapping:
        return "", ["invalid_sex"]
    return mapping[normalized], []


def _validate_age(value: str) -> Tuple[str, List[str]]:
    match = AGE_PATTERN.fullmatch(_compact(value))
    if not match:
        return "", ["invalid_age"]
    age = int(match.group(1))
    if not 0 <= age <= 130:
        return "", ["age_out_of_range"]
    return str(age), []


def _validate_date(value: str, field: str) -> Tuple[str, List[str]]:
    match = DATE_PATTERN.search(_compact(value))
    if not match:
        return "", ["invalid_%s" % field]
    try:
        parsed = dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return "", ["invalid_%s" % field]
    return parsed.isoformat(), []


def _validate_exam_item(value: str) -> Tuple[str, List[str]]:
    normalized = " ".join(value.split())
    compact = _compact(normalized)
    reasons: List[str] = []
    if compact in LABEL_TEXTS:
        reasons.append("label_selected_as_value")
    if len(compact) < 2:
        reasons.append("invalid_exam_item")
    return normalized, reasons


def _normalize_field(field: str, value: str) -> Tuple[str, List[str]]:
    raw_value = value
    value = _strip_field_label(value, field)
    if not value:
        if _compact(raw_value) in LABEL_TEXTS:
            return "", ["label_selected_as_value"]
        return "", []
    if field == "patient_name":
        return _validate_name(value)
    if field in IDENTIFIER_FIELDS:
        return _validate_identifier(value, field)
    if field == "sex":
        return _validate_sex(value)
    if field == "age":
        return _validate_age(value)
    if field in {"birthday", "report_date"}:
        return _validate_date(value, field)
    if field == "exam_item":
        return _validate_exam_item(value)
    raise ValueError("unsupported field: %s" % field)


def is_plausible_field_value(field: str, value: str) -> bool:
    """Return whether one OCR span can be a syntactically valid field value.

    This is only a candidate-reduction gate for the desktop constrained-choice
    experiment. Final acceptance still goes through materialize_and_validate.
    """

    normalized, reasons = _normalize_field(field, value)
    return bool(normalized) and not reasons


def _age_from_birthday(birthday: str, today: dt.date) -> int:
    born = dt.date.fromisoformat(birthday)
    years = today.year - born.year
    if (today.month, today.day) < (born.month, born.day):
        years -= 1
    return years


def materialize_and_validate(
    links: Mapping[str, Sequence[int]],
    spans: Sequence[OcrSpan],
    settings: ValidationSettings,
    today: Optional[dt.date] = None,
    label_links: Optional[Mapping[str, Sequence[int]]] = None,
    value_modes: Optional[Mapping[str, str]] = None,
) -> Tuple[Dict[str, FieldEvidence], List[str]]:
    """Build evidence-backed field values and return global rejection reasons."""

    fields = empty_fields()
    by_id = {span.id: span for span in spans}
    used_by: Dict[int, str] = {}
    global_reasons: List[str] = []

    for field in FIELD_NAMES:
        selected_ids = list(links.get(field, []))
        selected_label_ids = list((label_links or {}).get(field, []))
        selected: List[OcrSpan] = []
        reasons: List[str] = []
        for span_id in selected_ids:
            span = by_id.get(span_id)
            if span is None:
                reasons.append("unknown_span_id:%s" % span_id)
                continue
            previous = used_by.get(span_id)
            if previous is not None and previous != field:
                reasons.append("span_reused_by:%s" % previous)
                continue
            used_by[span_id] = field
            selected.append(span)

        raw_value = _join_selected(selected)
        if (value_modes or {}).get(field) == "after_delimiter":
            raw_value = extract_after_delimiter(raw_value)
            if not raw_value:
                reasons.append("invalid_after_delimiter_value")
        normalized_value, value_reasons = _normalize_field(field, raw_value)
        reasons.extend(value_reasons)
        fields[field] = FieldEvidence(
            value=normalized_value if not reasons else "",
            label_span_ids=selected_label_ids,
            source_span_ids=selected_ids,
            ocr_confidence=(sum(item.score for item in selected) / len(selected) if selected else 0.0),
            validation_ok=not reasons,
            validation_reasons=reasons,
        )
        if reasons:
            global_reasons.extend("%s:%s" % (field, reason) for reason in reasons)

    if settings.require_patient_name and not fields["patient_name"].value:
        global_reasons.append("missing_patient_name")
    if settings.require_identifier and not any(fields[field].value for field in IDENTIFIER_FIELDS):
        global_reasons.append("missing_patient_identifier")

    birthday = fields["birthday"].value
    age = fields["age"].value
    if birthday and age:
        expected_age = _age_from_birthday(birthday, today or dt.date.today())
        if abs(int(age) - expected_age) > settings.max_age_delta_years:
            global_reasons.append("age_birthday_conflict")
            fields["age"] = replace(
                fields["age"],
                value="",
                validation_ok=False,
                validation_reasons=fields["age"].validation_reasons + ["age_birthday_conflict"],
            )

    report_date = fields["report_date"].value
    if report_date and dt.date.fromisoformat(report_date) > (today or dt.date.today()) + dt.timedelta(days=366):
        global_reasons.append("report_date_implausible")
        fields["report_date"] = replace(
            fields["report_date"],
            value="",
            validation_ok=False,
            validation_reasons=fields["report_date"].validation_reasons + ["report_date_implausible"],
        )

    return fields, sorted(set(global_reasons))
