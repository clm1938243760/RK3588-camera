#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

import yaml


def main() -> None:
    parser = argparse.ArgumentParser(description="Enable camera/HID-only RK3588 report center mode")
    parser.add_argument("--config", default="/opt/rk3588_gateway/config.yaml")
    args = parser.parse_args()
    path = Path(args.config)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("configuration root must be a mapping")

    center = data.setdefault("report_center", {})
    center["enabled"] = True
    center["shadow_mode"] = False
    center["intake_only"] = True
    center["hid_active_marker"] = "/run/rk3588-report-center/hid-active"
    center["external_report_token_file"] = "/etc/gadget-msc-printer/report-link.token"
    center["external_report_window_seconds"] = 7200

    for section_name in ("scanner", "print_capture", "msc"):
        section = data.setdefault(section_name, {})
        if isinstance(section, dict):
            section["enabled"] = False

    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


if __name__ == "__main__":
    main()
