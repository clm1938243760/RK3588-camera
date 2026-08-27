from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List


class ManifestError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check_manifest(path: Path) -> Dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError("runtime manifest not found: %s" % path) from exc
    except json.JSONDecodeError as exc:
        raise ManifestError("runtime manifest is invalid JSON") from exc
    if not isinstance(manifest, dict):
        raise ManifestError("runtime manifest root must be an object")

    errors: List[str] = []
    if manifest.get("platform") != "rk3588":
        errors.append("platform must be rk3588")
    if manifest.get("status") != "verified":
        errors.append("manifest status must be verified")

    for section_name, key in (("rkllm", "runtime_path"), ("model", "path")):
        section = manifest.get(section_name)
        if not isinstance(section, dict):
            errors.append("missing %s section" % section_name)
            continue
        value = str(section.get(key, "") or "")
        expected_hash = str(section.get("sha256", "") or "")
        if not value or value == "UNSET":
            errors.append("%s.%s is unset" % (section_name, key))
            continue
        artifact = Path(value)
        if not artifact.is_file():
            errors.append("missing artifact: %s" % artifact)
            continue
        if not expected_hash or expected_hash == "UNSET":
            errors.append("%s.sha256 is unset" % section_name)
        elif _sha256(artifact).lower() != expected_hash.lower():
            errors.append("SHA-256 mismatch: %s" % artifact)

    rkllm = manifest.get("rkllm")
    if not isinstance(rkllm, dict) or str(rkllm.get("sdk_version", "")) in {"", "UNSET"}:
        errors.append("rkllm.sdk_version is unset")

    model = manifest.get("model")
    if not isinstance(model, dict) or model.get("target_platform") != "rk3588":
        errors.append("model.target_platform must be rk3588")

    ppocr = manifest.get("ppocr")
    if not isinstance(ppocr, dict):
        errors.append("missing ppocr section")
    else:
        if str(ppocr.get("service_version", "")) in {"", "UNSET"}:
            errors.append("ppocr.service_version is unset")
        for artifact_name, path_key, hash_key in (
            ("det", "det_model_path", "det_model_sha256"),
            ("rec", "rec_model_path", "rec_model_sha256"),
        ):
            value = str(ppocr.get(path_key, "") or "")
            expected_hash = str(ppocr.get(hash_key, "") or "")
            if not value or value == "UNSET":
                errors.append("ppocr.%s is unset" % path_key)
                continue
            artifact = Path(value)
            if not artifact.is_file():
                errors.append("missing PP-OCR %s artifact: %s" % (artifact_name, artifact))
                continue
            if not expected_hash or expected_hash == "UNSET":
                errors.append("ppocr.%s is unset" % hash_key)
            elif _sha256(artifact).lower() != expected_hash.lower():
                errors.append("SHA-256 mismatch: %s" % artifact)

    return {"ok": not errors, "manifest": manifest, "errors": errors}
