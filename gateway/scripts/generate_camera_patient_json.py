#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json

from rk3588_gateway.report_center.camera_patient import (
    CameraPatientResolver,
    default_camera_patient_fields,
)
from rk3588_gateway.report_center.store import ReportCenterStore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate and persist patient JSON from a stored camera OCR capture"
    )
    parser.add_argument(
        "--database",
        default="/var/lib/rk3588-report-center/db/report-center.sqlite3",
    )
    parser.add_argument("--capture-id", default="", help="defaults to the latest capture")
    parser.add_argument(
        "--enable-auto",
        action="store_true",
        help="publish default field rules and enable automatic processing",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    store = ReportCenterStore(args.database)
    profile = store.active_profile_revision()
    if args.enable_auto:
        config = copy.deepcopy(profile["config"])
        config["camera_patient_enabled"] = True
        resolver = config.get("field_resolver")
        if not isinstance(resolver, dict) or not isinstance(resolver.get("fields"), list) or not resolver["fields"]:
            config["field_resolver"] = {
                "provider": "rules",
                "fields": default_camera_patient_fields(),
            }
        revision_id = store.save_profile_draft(
            int(profile["profile_id"]), config, "camera-json-cli"
        )
        store.publish_profile(
            int(profile["profile_id"]), "camera-json-cli", revision_id=revision_id
        )
        profile = store.active_profile_revision()
    resolver_config = profile["config"].get("field_resolver", {})
    if not isinstance(resolver_config, dict):
        resolver_config = {"provider": "rules", "fields": []}

    capture_id = args.capture_id.strip()
    if not capture_id:
        captures = store.list_camera_captures(1)
        if not captures:
            raise SystemExit("no stored camera OCR capture")
        capture_id = str(captures[0]["capture_id"])

    capture = store.get_camera_capture(capture_id)
    fields = resolver_config.get("fields", [])
    if not isinstance(fields, list):
        fields = []
    result = CameraPatientResolver().resolve(capture["payload"], fields)
    saved, created = store.record_camera_patient_result(
        capture_id,
        int(profile["id"]),
        resolver_config,
        result,
        "camera-json-cli",
    )
    record = result["response"].get("data", [{}])
    patient = record[0] if record else {}
    print(json.dumps({
        "capture_id": capture_id,
        "status": result["status"],
        "fields_present": [key for key, value in patient.items() if value not in (None, "")],
        "result_id": saved["id"],
        "created": created,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
