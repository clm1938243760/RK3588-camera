from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PATIENT_KEYS = [
    "exam_item",
    "his_exam_no",
    "report_no",
    "patient_id",
    "patient_name",
    "name_phonetic",
    "xing",
    "ming",
    "age",
    "nian",
    "yue",
    "ri",
    "birthday",
]


def normalized_sex(value: Any) -> str:
    text = str(value or "").strip()
    if text in {"1", "M", "m", "male", "Male"}:
        return "男"
    if text in {"2", "F", "f", "female", "Female"}:
        return "女"
    return text


def normalized_patient(record: dict[str, Any]) -> dict[str, str]:
    patient = {key: str(record.get(key, "") or "") for key in PATIENT_KEYS}
    patient["sex"] = normalized_sex(record.get("sex", ""))
    return patient


def build_form_task(scan: str, record: dict[str, Any], template_path: str) -> dict[str, Any]:
    template = json.loads(Path(template_path).read_text(encoding="utf-8"))
    patient = normalized_patient(record)
    events = []

    for raw_event in template.get("eventClassList", []):
        click_type = int(raw_event.get("clickType", -1))
        text = raw_event.get("text")
        event = {
            "index": int(raw_event.get("index", len(events))),
            "clickType": click_type,
            "x": int(raw_event.get("x", 0)),
            "y": int(raw_event.get("y", 0)),
        }
        if click_type == 1 and text:
            field = str(text)
            event["field"] = field
            event["value"] = patient.get(field, "")
        elif click_type == 7 and text:
            raw_text = str(text)
            expected = raw_text.split(":", 1)[1] if ":" in raw_text else raw_text
            event["condition"] = {"field": "sex", "equals": expected}
        elif text:
            event["text"] = str(text)
        events.append(event)

    return {
        "scan_text": scan,
        "patient": patient,
        "title": template.get("title", ""),
        "windowTitleLocation": template.get("windowTitleLocation", ""),
        "eventClassList": events,
    }
