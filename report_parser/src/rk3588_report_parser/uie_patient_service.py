"""Camera/OCR to UIE patient JSON orchestration for the desktop baseline."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence

from PIL import Image, ImageOps, UnidentifiedImageError

from .clients import LocalPpOcrClient, OcrClientProtocol
from .settings import OcrSettings
from .uie_extraction import (
    build_patient_response,
    build_evidence_document,
    blocks_from_payload,
    normalize_uie_schema,
    run_uie_extraction,
    uie_prompts,
)


MAX_CAPTURE_JSON_BYTES = 5 * 1024 * 1024


class UieEngineProtocol(Protocol):
    def predict(self, text: str) -> Mapping[str, Any]:
        ...

    def set_prompts(self, prompts: Sequence[str]) -> None:
        ...


class UiePatientServiceError(RuntimeError):
    pass


class CameraCaptureFileWatcher:
    """Feed each final camera OCR capture into UIE exactly once."""

    def __init__(
        self,
        service: "UiePatientService",
        result_path: Path,
        poll_seconds: float = 0.5,
    ) -> None:
        if poll_seconds < 0.1:
            raise ValueError("camera result poll interval must be at least 0.1 seconds")
        self.service = service
        self.result_path = result_path
        self.poll_seconds = float(poll_seconds)
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="uie-camera-result-watcher",
            daemon=True,
        )
        self._last_fingerprint: Optional[tuple[int, int]] = None
        self._completed_capture_id = ""

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=max(2.0, self.poll_seconds * 3))

    def _run(self) -> None:
        while not self._stop_event.wait(self.poll_seconds):
            try:
                stat = self.result_path.stat()
            except FileNotFoundError:
                continue
            except OSError:
                continue
            fingerprint = (int(stat.st_mtime_ns), int(stat.st_size))
            if fingerprint == self._last_fingerprint:
                continue
            try:
                payload = json.loads(self.result_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, Mapping):
                self._last_fingerprint = fingerprint
                continue
            capture_id = str(payload.get("capture_id", "")).strip()
            status = str(payload.get("status", "")).strip()
            if not capture_id or status not in {"accepted", "review_required"}:
                self._last_fingerprint = fingerprint
                continue
            if capture_id == self._completed_capture_id:
                self._last_fingerprint = fingerprint
                continue
            try:
                result = self.service.parse_capture(payload)
            except UiePatientServiceError:
                continue
            except (OSError, ValueError, RuntimeError):
                self._last_fingerprint = fingerprint
                print(
                    "camera UIE capture failed capture_id=%s" % capture_id[:12],
                    flush=True,
                )
                continue
            self._completed_capture_id = capture_id
            self._last_fingerprint = fingerprint
            print(
                "camera UIE capture completed capture_id=%s status=%s"
                % (capture_id[:12], result.get("status", "error")),
                flush=True,
            )


class UiePatientService:
    def __init__(
        self,
        engine: UieEngineProtocol,
        schema: Sequence[Mapping[str, Any]],
        model: str,
        ocr_settings: OcrSettings,
        ocr_client: Optional[OcrClientProtocol] = None,
        schema_path: Optional[Path] = None,
        result_path: Optional[Path] = None,
        queue_size: int = 1,
    ) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self.engine = engine
        self.model = str(model).strip()
        if not self.model:
            raise ValueError("UIE model must not be empty")
        self.ocr_settings = ocr_settings
        self.ocr_client = ocr_client or LocalPpOcrClient()
        self.schema_path = schema_path
        self.result_path = result_path
        self._schema = normalize_uie_schema({"fields": list(schema)})
        self._inference_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(queue_size)
        self._latest: Optional[dict[str, Any]] = None
        self._capture_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def parse_image(self, image_bytes: bytes) -> dict[str, Any]:
        if not image_bytes:
            raise ValueError("image is empty")
        try:
            with Image.open(io.BytesIO(image_bytes)) as source:
                image = ImageOps.exif_transpose(source)
                image.load()
                image_size = (int(image.width), int(image.height))
        except (UnidentifiedImageError, OSError) as exc:
            raise ValueError("image cannot be decoded") from exc
        if image_size[0] < 32 or image_size[1] < 32:
            raise ValueError("image is too small")

        digest = hashlib.sha256(image_bytes).hexdigest()
        capture_id = "upload-%s" % digest[:24]
        started = time.monotonic()
        if not self._slots.acquire(blocking=False):
            raise UiePatientServiceError("UIE inference queue is full")
        try:
            with self._inference_lock:
                ocr_started = time.monotonic()
                response = self.ocr_client.recognize(image_bytes, self.ocr_settings)
                ocr_ms = (time.monotonic() - ocr_started) * 1000.0
                blocks = _normalize_image_blocks(blocks_from_payload(response), image_size)
                return self._extract_and_store(
                    blocks,
                    capture_id=capture_id,
                    image_sha256=digest,
                    image_size=image_size,
                    source_type="uploaded_image",
                    ocr_ms=ocr_ms,
                    total_started=started,
                )
        finally:
            self._slots.release()

    def parse_capture(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        capture_id = str(payload.get("capture_id", "")).strip()
        if not capture_id or len(capture_id) > 128:
            raise ValueError("capture_id is required")
        status = str(payload.get("status", "")).strip()
        if status not in {"accepted", "review_required"}:
            raise ValueError("camera OCR result is not final")
        serialized_size = len(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if serialized_size > MAX_CAPTURE_JSON_BYTES:
            raise ValueError("camera OCR result exceeds 5 MiB")

        if not self._slots.acquire(blocking=False):
            raise UiePatientServiceError("UIE inference queue is full")
        try:
            with self._inference_lock:
                cached = self._capture_cache.get(capture_id)
                if cached is not None:
                    self._capture_cache.move_to_end(capture_id)
                    return copy.deepcopy(cached)
                blocks = blocks_from_payload(payload)
                source = payload.get("source") if isinstance(payload.get("source"), Mapping) else {}
                image_size = _image_size_from_capture(payload, source)
                digest = str(source.get("selected_frame_sha256") or payload.get("image_sha256") or "")
                return self._extract_and_store(
                    blocks,
                    capture_id=capture_id,
                    image_sha256=digest,
                    image_size=image_size,
                    source_type="camera_ocr_schema_v2",
                    ocr_ms=_capture_ocr_ms(payload),
                    total_started=time.monotonic(),
                    capture_quality=payload.get("quality"),
                )
        finally:
            self._slots.release()

    def get_schema(self) -> dict[str, Any]:
        with self._inference_lock:
            return {
                "schema_version": 1,
                "model": self.model,
                "fields": copy.deepcopy(self._schema),
            }

    def update_schema(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        normalized = normalize_uie_schema(payload)
        prompts = uie_prompts(normalized)
        with self._inference_lock:
            self.engine.set_prompts(prompts)
            self._schema = normalized
            if self.schema_path is not None:
                _atomic_write_json(
                    self.schema_path,
                    {"schema_version": 1, "fields": normalized},
                )
        return self.get_schema()

    def latest(self) -> Optional[dict[str, Any]]:
        with self._inference_lock:
            return copy.deepcopy(self._latest)

    def select_candidate(self, field_key: str, candidate_index: int) -> dict[str, Any]:
        key = str(field_key).strip()
        if not key:
            raise ValueError("field_key is required")
        if not isinstance(candidate_index, int) or isinstance(candidate_index, bool):
            raise ValueError("candidate_index must be an integer")
        with self._inference_lock:
            if self._latest is None:
                raise ValueError("there is no result to correct")
            result = copy.deepcopy(self._latest)
            field = result.get("fields", {}).get(key)
            if not isinstance(field, Mapping):
                raise ValueError("field does not have OCR evidence")
            primary = {name: copy.deepcopy(value) for name, value in field.items() if name != "alternatives"}
            raw_alternatives = field.get("alternatives", [])
            alternatives = [
                copy.deepcopy(dict(value))
                for value in raw_alternatives
                if isinstance(value, Mapping)
            ]
            candidates = [primary, *alternatives]
            if candidate_index < 0 or candidate_index >= len(candidates):
                raise ValueError("candidate_index is out of range")
            selected = candidates[candidate_index]
            selected["alternatives"] = [
                copy.deepcopy(value)
                for index, value in enumerate(candidates)
                if index != candidate_index
            ]
            result["fields"][key] = selected
            result["conflict_fields"] = [
                value for value in result.get("conflict_fields", []) if value != key
            ]
            result["review_fields"] = [
                value for value in result.get("review_fields", []) if value != key
            ]
            result.setdefault("manual_corrections", {})[key] = {
                "source_span_ids": copy.deepcopy(selected.get("source_span_ids", [])),
                "selected_at": int(time.time()),
            }
            result["updated_at"] = int(time.time())
            result["status"] = _status_after_correction(result)
            result["patient_response"] = build_patient_response(
                result["fields"], result["status"]
            )
            self._store_result(result)
            return copy.deepcopy(result)

    def runtime_summary(self) -> dict[str, Any]:
        with self._inference_lock:
            summary = {
                "model": self.model,
                "ocr_backend": "local_ppocr",
                "ocr_endpoint": self.ocr_settings.endpoint,
                "field_count": len(self._schema),
                "evidence_policy": "exact_ocr_substring",
                "image_retention": "none",
            }
            runtime_info = getattr(self.engine, "runtime_info", None)
            if isinstance(runtime_info, Mapping):
                summary.update(copy.deepcopy(dict(runtime_info)))
            return summary

    def _extract_and_store(
        self,
        blocks: Sequence[Mapping[str, Any]],
        *,
        capture_id: str,
        image_sha256: str,
        image_size: tuple[int, int],
        source_type: str,
        ocr_ms: float,
        total_started: float,
        capture_quality: Any = None,
    ) -> dict[str, Any]:
        if not blocks:
            raise ValueError("OCR result does not contain text blocks")
        schema = copy.deepcopy(self._schema)
        result = run_uie_extraction(blocks, schema, self.model, self.engine.predict)
        document = build_evidence_document(blocks)
        result["capture_id"] = capture_id
        result["created_at"] = int(time.time())
        result["image_sha256"] = image_sha256
        result["source"].update({
            "type": source_type,
            "image_size": [image_size[0], image_size[1]],
        })
        if isinstance(capture_quality, Mapping):
            result["quality"] = copy.deepcopy(dict(capture_quality))
        else:
            scores = [float(value.get("score", 0.0)) for value in blocks]
            result["quality"] = {
                "ocr_block_count": len(blocks),
                "ocr_mean_score": round(sum(scores) / len(scores), 4) if scores else 0.0,
            }
        result["document"] = {
            "schema_version": 2,
            "image_size": [image_size[0], image_size[1]],
            "full_text": document.text,
            "blocks": [copy.deepcopy(dict(value)) for value in blocks],
        }
        result["timings"]["ocr_ms"] = round(float(ocr_ms), 2)
        result["timings"]["total_ms"] = round(
            (time.monotonic() - total_started) * 1000.0, 2
        )
        self._store_result(result)
        return result

    def _store_result(self, result: Mapping[str, Any]) -> None:
        stored = copy.deepcopy(dict(result))
        self._latest = stored
        capture_id = str(stored.get("capture_id", ""))
        if capture_id:
            self._capture_cache[capture_id] = copy.deepcopy(stored)
            self._capture_cache.move_to_end(capture_id)
            while len(self._capture_cache) > 32:
                self._capture_cache.popitem(last=False)
        if self.result_path is not None:
            _atomic_write_json(self.result_path, stored)


def _normalize_image_blocks(
    blocks: Sequence[Mapping[str, Any]], image_size: tuple[int, int]
) -> list[dict[str, Any]]:
    width, height = image_size
    normalized = []
    for value in blocks:
        block = copy.deepcopy(dict(value))
        box = _valid_box(block.get("box"))
        if box is not None:
            block["box"] = box
            block["normalized_box"] = [
                max(0, min(1000, round(box[0] * 1000 / width))),
                max(0, min(1000, round(box[1] * 1000 / height))),
                max(0, min(1000, round(box[2] * 1000 / width))),
                max(0, min(1000, round(box[3] * 1000 / height))),
            ]
        normalized.append(block)
    return normalized


def _valid_box(value: Any) -> Optional[list[int]]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        box = [int(round(float(item))) for item in value]
    except (TypeError, ValueError):
        return None
    if box[2] < box[0] or box[3] < box[1]:
        return None
    return box


def _image_size_from_capture(
    payload: Mapping[str, Any], source: Mapping[str, Any]
) -> tuple[int, int]:
    candidates = [
        source.get("frame_size"),
        source.get("image_size"),
        (payload.get("document") or {}).get("image_size")
        if isinstance(payload.get("document"), Mapping)
        else None,
    ]
    for value in candidates:
        if isinstance(value, Mapping):
            value = [value.get("width"), value.get("height")]
        if isinstance(value, (list, tuple)) and len(value) == 2:
            try:
                width, height = int(value[0]), int(value[1])
            except (TypeError, ValueError):
                continue
            if width > 0 and height > 0:
                return width, height
    return (1000, 1000)


def _capture_ocr_ms(payload: Mapping[str, Any]) -> float:
    timings = payload.get("timings")
    if not isinstance(timings, Mapping):
        return 0.0
    for key in ("ocr_ms", "ocr_primary_ms", "total_ocr_ms"):
        try:
            value = float(timings.get(key, 0.0))
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return value
    return 0.0


def _status_after_correction(result: Mapping[str, Any]) -> str:
    fields = result.get("fields")
    if not isinstance(fields, Mapping) or not fields:
        return "rejected"
    if (
        result.get("missing_fields")
        or result.get("conflict_fields")
        or result.get("review_fields")
    ):
        return "review_required"
    return "accepted"


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
