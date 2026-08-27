from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional


DEFAULT_DISPLAY_STATE = {
    "screen": "wait_scan",
    "patient_name": "",
    "patient_id": "",
}


def publish_display_state(
    path: str,
    display: dict[str, Any],
    *,
    leading_display: Optional[dict[str, Any]] = None,
    display_not_before: Optional[float] = None,
    expires_at: Optional[float] = None,
) -> None:
    """Publish the small shared state file consumed by the SPI display service."""
    if not path:
        return
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "display": {**DEFAULT_DISPLAY_STATE, **display},
        "updated_at": time.time(),
    }
    if leading_display is not None:
        payload["leading_display"] = {**DEFAULT_DISPLAY_STATE, **leading_display}
    if display_not_before is not None:
        payload["display_not_before"] = display_not_before
    if expires_at is not None:
        payload["expires_at"] = expires_at
    temporary = destination.with_name(".%s.%d.tmp" % (destination.name, os.getpid()))
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        try:
            os.chmod(destination, 0o644)
        except OSError:
            pass
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def read_display_state(path: str) -> Optional[dict[str, Any]]:
    if not path:
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None
    display = payload.get("display") if isinstance(payload, dict) else None
    if not isinstance(display, dict):
        return None
    return {**DEFAULT_DISPLAY_STATE, **display}
