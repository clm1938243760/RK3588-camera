from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from .domain import NotFoundError, ValidationError


CAPTURE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MAX_CONFIGURATION_IMAGE_BYTES = 25 * 1024 * 1024


def build_runtime_template(profile_revision: dict[str, Any]) -> dict[str, Any]:
    config = profile_revision.get("config", {})
    resolver = config.get("field_resolver", {}) if isinstance(config, dict) else {}
    fields = resolver.get("fields", []) if isinstance(resolver, dict) else []
    fixed_fields = []
    for field in fields if isinstance(fields, list) else []:
        if not isinstance(field, dict) or not bool(field.get("enabled", True)):
            continue
        if str(field.get("match_mode", "")) != "fixed_roi":
            continue
        roi = field.get("roi")
        if not valid_normalized_roi(roi):
            continue
        fixed_fields.append({
            "field_key": str(field.get("field_key", ""))[:64],
            "enabled": True,
            "required": bool(field.get("required", False)),
            "roi": [round(float(value), 3) for value in roi],
            "min_ocr_score": max(0.0, min(1.0, float(field.get("min_ocr_score", 0.0)))),
            "expand_once": bool(field.get("expand_once", True)),
            "expand_ratio": max(0.0, min(0.5, float(field.get("expand_ratio", 0.10)))),
        })
    template = config.get("camera_template", {}) if isinstance(config, dict) else {}
    if not isinstance(template, dict):
        template = {}
    return {
        "schema_version": 1,
        "enabled": bool(config.get("camera_patient_enabled", False)) and bool(fixed_fields),
        "mode": "fixed_roi",
        "profile_revision_id": int(profile_revision.get("id", 0)),
        "template_id": str(template.get("id", "default"))[:64],
        "template_name": str(template.get("name", "申请单1"))[:100],
        "selection_mode": "manual",
        "reference_capture_id": str(template.get("reference_capture_id", ""))[:128],
        "canonical_image_size": template.get("canonical_image_size", []),
        "fields": fixed_fields,
    }


def write_runtime_template(path: str, profile_revision: dict[str, Any]) -> dict[str, Any]:
    payload = build_runtime_template(profile_revision)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(destination.parent, 0o700)
    _camera_runtime_owner(destination.parent)
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
        os.chmod(destination, 0o600)
        _camera_runtime_owner(destination)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return payload


def request_full_page_once(path: str, actor: str = "") -> dict[str, Any]:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(destination.parent, 0o700)
    _camera_runtime_owner(destination.parent)
    payload = {
        "schema_version": 1,
        "requested_at": time.time(),
        "requested_by": str(actor)[:64],
    }
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
        os.chmod(destination, 0o600)
        _camera_runtime_owner(destination)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return {"armed": True, "requested_at": payload["requested_at"]}


def full_page_once_status(path: str) -> dict[str, Any]:
    marker = Path(path)
    try:
        stat = marker.stat()
    except FileNotFoundError:
        return {"armed": False, "requested_at": None}
    if not marker.is_file():
        return {"armed": False, "requested_at": None}
    return {"armed": True, "requested_at": stat.st_mtime}


def find_configuration_image(
    capture_id: str,
    runtime_dir: str,
    retained_dir: str,
) -> Optional[Path]:
    _validate_capture_id(capture_id)
    for root in (Path(retained_dir), Path(runtime_dir)):
        candidate = root / (capture_id + ".jpg")
        if candidate.is_file() and candidate.stat().st_size <= MAX_CONFIGURATION_IMAGE_BYTES:
            return candidate
    return None


def retain_configuration_image(
    capture_id: str,
    runtime_dir: str,
    retained_dir: str,
    expected_sha256: str = "",
) -> Path:
    _validate_capture_id(capture_id)
    retained_root = Path(retained_dir)
    retained_root.mkdir(parents=True, exist_ok=True)
    os.chmod(retained_root, 0o700)
    destination = retained_root / (capture_id + ".jpg")
    if destination.is_file():
        _verify_image(destination, expected_sha256)
        return destination
    source = Path(runtime_dir) / (capture_id + ".jpg")
    if not source.is_file():
        raise NotFoundError("configuration image is unavailable for this capture")
    _verify_image(source, expected_sha256)
    temporary = retained_root / (".%s.%d.tmp" % (capture_id, os.getpid()))
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            shutil.copyfileobj(reader, writer, 1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return destination


def retain_entry_image(
    capture_id: str,
    runtime_dir: str,
    retained_dir: str,
    entry_dir: str,
    expected_sha256: str = "",
) -> Path:
    """Copy the OCR frame into durable entry history before /run is cleaned."""
    _validate_capture_id(capture_id)
    destination_root = Path(entry_dir)
    destination_root.mkdir(parents=True, exist_ok=True)
    os.chmod(destination_root, 0o700)
    destination = destination_root / (capture_id + ".jpg")
    if destination.is_file():
        _verify_image(destination, expected_sha256)
        return destination
    source = find_configuration_image(capture_id, runtime_dir, retained_dir)
    if source is None:
        raise NotFoundError("entry capture image is unavailable for this capture")
    _verify_image(source, expected_sha256)
    temporary = destination_root / (".%s.%d.tmp" % (capture_id, os.getpid()))
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            shutil.copyfileobj(reader, writer, 1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return destination


def valid_normalized_roi(value: Any) -> bool:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return False
    try:
        left, top, right, bottom = (float(item) for item in value)
    except (TypeError, ValueError):
        return False
    return 0 <= left < right <= 1000 and 0 <= top < bottom <= 1000


def _validate_capture_id(capture_id: str) -> None:
    if not CAPTURE_ID_PATTERN.fullmatch(capture_id):
        raise ValidationError("invalid camera capture ID")


def _verify_image(path: Path, expected_sha256: str) -> None:
    size = path.stat().st_size
    if size < 4 or size > MAX_CONFIGURATION_IMAGE_BYTES:
        raise ValidationError("configuration image size is invalid")
    if expected_sha256:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected_sha256:
            raise ValidationError("configuration image does not match OCR evidence")


def _camera_runtime_owner(path: Path) -> None:
    try:
        import grp
        import pwd
        user = pwd.getpwnam("linaro")
        group = grp.getgrnam("linaro")
        os.chown(path, user.pw_uid, group.gr_gid)
    except (ImportError, KeyError, PermissionError, OSError):
        pass
