#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from rk3588_gateway.config import load_config
from rk3588_gateway.report_center.config import load_report_center_config
from rk3588_gateway.report_center.store import ReportCenterStore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Publish the BodyPass 1920x1080 fixed absolute HID workflow"
    )
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument(
        "--actions",
        default=str(ROOT / "profiles" / "bodypass_fixed_hid.json"),
    )
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def load_hid_definition(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("coordinate_mode") != "fixed_absolute":
        raise ValueError("actions file must define fixed_absolute HID mode")
    basis = value.get("coordinate_basis", {})
    actions = value.get("actions", [])
    if not isinstance(basis, dict) or int(basis.get("width", 0)) < 1 or int(basis.get("height", 0)) < 1:
        raise ValueError("actions file has an invalid coordinate basis")
    if not isinstance(actions, list) or not actions:
        raise ValueError("actions file must contain at least one action")
    return value


def main() -> None:
    args = parse_args()
    app_config = load_config(args.config)
    center_config = load_report_center_config(args.config)
    definition = load_hid_definition(Path(args.actions))
    basis = definition["coordinate_basis"]
    if (
        int(basis["width"]) != int(app_config.hid_input.screen_width)
        or int(basis["height"]) != int(app_config.hid_input.screen_height)
    ):
        raise ValueError("fixed HID coordinate basis does not match hid_input screen size")

    store = ReportCenterStore(center_config.database_path)
    active = store.active_profile_revision()
    updated = copy.deepcopy(active["config"])
    current_hid = updated.get("hid", {})
    if not isinstance(current_hid, dict):
        current_hid = {}
    current_hid.update(definition)
    updated["hid"] = current_hid

    result = {
        "dry_run": not args.execute,
        "profile_id": active["profile_id"],
        "profile_name": active["name"],
        "previous_version": active["version"],
        "coordinate_mode": definition["coordinate_mode"],
        "coordinate_basis": basis,
        "action_count": len(definition["actions"]),
    }
    if args.execute:
        revision_id = store.save_profile_draft(
            int(active["profile_id"]), updated, "fixed-hid-migration"
        )
        store.publish_profile(
            int(active["profile_id"]), "fixed-hid-migration", revision_id
        )
        result["published_revision_id"] = revision_id
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
