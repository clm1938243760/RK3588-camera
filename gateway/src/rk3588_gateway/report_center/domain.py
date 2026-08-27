from __future__ import annotations

import re
from typing import Any


STANDARD_PATIENT_FIELDS = (
    "birthday",
    "exam_item",
    "ming",
    "sex",
    "yue",
    "his_exam_no",
    "xing",
    "patient_id",
    "ri",
    "patient_name",
    "name_phonetic",
    "nian",
    "report_no",
    "age",
)

PATIENT_INPUT_MODES = {"scanner_query", "camera_query", "camera_direct", "manual"}
REPORT_SOURCES = {"msc", "printer"}
SESSION_STATES = {
    "queued",
    "resolving",
    "review_required",
    "entering",
    "entry_completed",
    "awaiting_report",
    "archiving",
    "completed",
    "cancelled",
    "report_missing",
    "error",
}
REPORT_STATES = {"incoming", "converting", "archived", "review_required", "invalid", "purged"}
UPLOAD_STATES = {"pending", "uploading", "retry_wait", "uploaded", "exhausted"}

SESSION_TRANSITIONS = {
    "queued": {"resolving", "entering", "review_required", "cancelled", "error"},
    "resolving": {"queued", "review_required", "cancelled", "error"},
    "review_required": {"queued", "entering", "cancelled", "error"},
    "entering": {"entry_completed", "awaiting_report", "queued", "cancelled", "error"},
    "entry_completed": {"completed", "report_missing", "cancelled", "error"},
    "awaiting_report": {"archiving", "report_missing", "cancelled", "error"},
    "archiving": {"completed", "awaiting_report", "review_required", "error"},
    "completed": set(),
    "cancelled": set(),
    "report_missing": set(),
    "error": {"queued", "review_required", "cancelled"},
}


class ReportCenterError(RuntimeError):
    pass


class ConflictError(ReportCenterError):
    pass


class NotFoundError(ReportCenterError):
    pass


class ValidationError(ReportCenterError):
    pass


def canonical_patient(record: dict[str, Any]) -> dict[str, Any]:
    patient = {field: record.get(field) for field in STANDARD_PATIENT_FIELDS}
    extra_fields = record.get("extra_fields", {})
    patient["extra_fields"] = dict(extra_fields) if isinstance(extra_fields, dict) else {}
    return patient


def validate_patient_mode(value: str) -> str:
    mode = str(value).strip()
    if mode not in PATIENT_INPUT_MODES:
        raise ValidationError("unsupported patient input mode")
    return mode


def validate_report_source(value: str) -> str:
    source = str(value).strip()
    if source not in REPORT_SOURCES:
        raise ValidationError("report source must be msc or printer")
    return source


def safe_filename_part(value: Any, fallback: str = "unknown") -> str:
    text = str(value or "").strip()
    text = re.sub(r"[\\/:*?\"<>|\x00-\x1f\x7f]+", "_", text)
    text = re.sub(r"\s+", " ", text).strip(" ._")
    return text[:80] or fallback
