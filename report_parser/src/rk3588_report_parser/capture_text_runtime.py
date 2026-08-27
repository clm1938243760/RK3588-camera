"""Asynchronous text-only camera runtime with a bounded latest-only OCR queue."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from .capture_orientation import prepare_document_jpeg
from .capture_region import (
    DocumentRecognitionRegion,
    crop_document_jpeg,
    remap_extraction_to_full_document,
)
from .capture_text import FullTextExtraction, FullTextExtractor, TextRefinementSettings
from .clients import LocalPpOcrClient
from .frame_quality import BurstQualitySelector, BurstSelection
from .frame_source import LatestJpegFrameSource
from .paper_detector import PaperDetection, create_docaligner_detector
from .paper_trigger import PaperObservation, PaperStabilityTracker, PaperTrackerConfig
from .settings import load_settings, with_endpoint_overrides


@dataclass(frozen=True)
class TextCaptureJob:
    capture_id: str
    selection: BurstSelection
    created_at: float


class LatestOnlyOcrWorker:
    """Run one OCR job and retain at most one replacement waiting behind it."""

    def __init__(self) -> None:
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="report-ocr")
        self.active: Optional[Tuple[str, Future[Any]]] = None
        self.pending: Optional[Tuple[str, Callable[[], Any]]] = None

    def submit(self, capture_id: str, callback: Callable[[], Any]) -> None:
        if self.active is None:
            self.active = (capture_id, self.executor.submit(callback))
        else:
            self.pending = (capture_id, callback)

    def poll(self) -> Optional[Tuple[str, Any, Optional[BaseException]]]:
        if self.active is None or not self.active[1].done():
            return None
        capture_id, future = self.active
        try:
            result = future.result()
            error = None
        except BaseException as exc:  # captured and converted to a non-sensitive error status
            result = None
            error = exc
        self.active = None
        if self.pending is not None:
            pending_id, callback = self.pending
            self.pending = None
            self.active = (pending_id, self.executor.submit(callback))
        return capture_id, result, error

    def discard_pending_except(self, capture_id: Optional[str]) -> None:
        if self.pending is not None and self.pending[0] != capture_id:
            self.pending = None

    def stage_for(self, capture_id: Optional[str]) -> Optional[str]:
        if capture_id and self.active is not None and self.active[0] == capture_id:
            return "active"
        if capture_id and self.pending is not None and self.pending[0] == capture_id:
            return "queued"
        return None

    def close(self) -> None:
        self.pending = None
        self.executor.shutdown(wait=True, cancel_futures=True)


def _paper_status(detection: PaperDetection, update: Any) -> Dict[str, Any]:
    return {
        "paper_detected": detection.detected,
        "paper_confidence": round(detection.confidence, 4),
        "paper_inference_ms": round(detection.inference_ms, 2),
        "frame_size": {"width": detection.frame_width, "height": detection.frame_height},
        "paper_corners": [[round(float(x), 2), round(float(y), 2)] for x, y in detection.corners],
        "state": update.state.value,
        "reason": update.reason,
        "stable_for": round(update.stable_for, 3),
        "observations": update.observations,
        "triggered": update.triggered,
    }


def _summary(extraction: FullTextExtraction) -> Dict[str, Any]:
    return extraction.public_status()


def _result_payload(
    job: TextCaptureJob,
    extraction: FullTextExtraction,
    ocr_rotation: int,
    target_long_side: int,
    ocr_image_sha256: str = "",
    recognition_mode: str = "full_page",
    recognition_region: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    frames = sorted(
        job.selection.frames,
        key=lambda frame: frame.quality.composite_score,
        reverse=True,
    )
    selected = frames[0]
    burst_ms = 0.0
    if len(job.selection.frames) > 1:
        burst_ms = (max(frame.timestamp for frame in job.selection.frames) - min(frame.timestamp for frame in job.selection.frames)) * 1000.0
    timings = dict(extraction.timings)
    timings["burst_capture_ms"] = burst_ms
    return {
        "status": extraction.status,
        "capture_id": job.capture_id,
        "created_at": job.created_at,
        "source": {
            "frame_size": {
                "width": selected.detection.frame_width,
                "height": selected.detection.frame_height,
            },
            "paper_corners": [
                [round(float(x), 3), round(float(y), 3)]
                for x, y in selected.detection.corners
            ],
            "ocr_rotation": ocr_rotation,
            "ocr_document_long_side": target_long_side,
            "selected_frame_sha256": hashlib.sha256(selected.image_bytes).hexdigest(),
            "ocr_image_sha256": ocr_image_sha256,
            "configuration_image_available": bool(ocr_image_sha256),
            "recognition_mode": recognition_mode,
            "recognition_region": dict(recognition_region or {}),
        },
        "quality": {
            "selected_frame": selected.quality.public_status(),
            "burst": job.selection.public_status(),
        },
        "timings": {key: round(value, 2) for key, value in timings.items()},
        "reasons": list(extraction.reasons),
        "document": None if extraction.document is None else extraction.document.to_dict(),
    }


def _failure_payload(job: TextCaptureJob, status: str, reason: str) -> Dict[str, Any]:
    selected = job.selection.best_frame
    return {
        "status": status,
        "capture_id": job.capture_id,
        "created_at": job.created_at,
        "source": {
            "frame_size": {
                "width": selected.detection.frame_width,
                "height": selected.detection.frame_height,
            },
            "paper_corners": [
                [round(float(x), 3), round(float(y), 3)]
                for x, y in selected.detection.corners
            ],
            "ocr_rotation": 0,
            "selected_frame_sha256": hashlib.sha256(selected.image_bytes).hexdigest(),
        },
        "quality": {"burst": job.selection.public_status()},
        "timings": {},
        "reasons": [reason],
        "document": None,
    }


def run_text_only(
    args: Any,
    write_status: Callable[[Any, Dict[str, Any]], None],
    write_result: Callable[[Any, Dict[str, Any]], None],
) -> int:
    detector = create_docaligner_detector(
        args.paper_model,
        args.object_threshold,
        args.detector_backend,
    )
    source = LatestJpegFrameSource(args.frame_glob)
    tracker = PaperStabilityTracker(
        PaperTrackerConfig(
            stable_seconds=args.stable_seconds,
            min_observations=args.min_observations,
            remove_seconds=args.remove_seconds,
        )
    )
    settings = with_endpoint_overrides(load_settings(args.config), args.ocr_endpoint, None)
    settings = replace(settings, ocr=replace(settings.ocr, timeout_seconds=args.ocr_timeout))
    recognition_region = DocumentRecognitionRegion(
        crop_top=args.ocr_region_crop_top,
        crop_bottom=args.ocr_region_crop_bottom,
        accept_top=args.ocr_region_accept_top,
        accept_bottom=args.ocr_region_accept_bottom,
    )
    extractor = FullTextExtractor(
        LocalPpOcrClient(),
        settings.ocr,
        TextRefinementSettings(
            max_regions=args.ocr_refinement_max_regions,
            max_duration_seconds=args.ocr_total_budget,
            primary_tile_max_aspect=args.ocr_tile_max_aspect,
            primary_tile_overlap_ratio=args.ocr_tile_overlap_ratio,
            primary_tile_max_count=args.ocr_tile_max_count,
        ),
    )
    worker = LatestOnlyOcrWorker()
    progress_lock = threading.Lock()
    progress: Dict[str, Dict[str, Any]] = {}
    processed = 0
    capture_id: Optional[str] = None
    burst_selector: Optional[BurstQualitySelector] = None
    burst_status: Optional[Dict[str, Any]] = None
    completed: Optional[Dict[str, Any]] = None
    last_signature = None
    result_count = 0

    write_result(
        args.result_file,
        {"status": "disabled", "mode": "text_only", "created_at": time.time()},
    )
    print(
        json.dumps(
            {
                "event": "started",
                "mode": "text_only",
                "frame_glob": args.frame_glob,
                "model_load_ms": round(detector.model_load_ms, 2),
                "detector_backend": detector.backend_name,
                "burst_frames": args.burst_frames,
                "ocr_document_long_side": args.ocr_document_long_side,
                "ocr_total_budget": args.ocr_total_budget,
                "recognition_region": recognition_region.to_dict(),
                "patient_images_saved": False,
            },
            ensure_ascii=True,
        ),
        flush=True,
    )

    def set_progress(job_id: str, stage: str, details: Dict[str, Any]) -> None:
        with progress_lock:
            progress[job_id] = {"stage": stage, **details}

    def execute(job: TextCaptureJob) -> Dict[str, Any]:
        ranked = sorted(
            job.selection.frames,
            key=lambda frame: frame.quality.composite_score,
            reverse=True,
        )
        primary = ranked[0]
        secondary = ranked[1] if len(ranked) > 1 else ranked[0]
        primary_document = prepare_document_jpeg(
            primary.image_bytes,
            primary.detection,
            degrees_counterclockwise=args.ocr_rotation,
            target_long_side=args.ocr_document_long_side,
        )
        secondary_document = prepare_document_jpeg(
            secondary.image_bytes,
            secondary.detection,
            degrees_counterclockwise=args.ocr_rotation,
            target_long_side=args.ocr_document_long_side,
        )
        image_sha256 = hashlib.sha256(primary_document).hexdigest()
        _write_configuration_image(
            args.configuration_image_dir, job.capture_id, primary_document
        )
        primary_crop = crop_document_jpeg(primary_document, recognition_region)
        secondary_crop = crop_document_jpeg(secondary_document, recognition_region)
        extraction = extractor.extract_refined(
            primary_crop.image_bytes,
            secondary_crop.image_bytes,
            progress=lambda stage, details: set_progress(job.capture_id, stage, details),
        )
        extraction = remap_extraction_to_full_document(
            extraction,
            primary_crop,
            low_confidence=extractor.refinement.low_confidence,
            low_mean_confidence=extractor.refinement.low_mean_confidence,
        )
        primary_ocr_calls = int(round(extraction.timings.get("primary_tile_count", 1.0)))
        secondary_full_ocr_calls = 1 if "secondary_full_ocr_ms" in extraction.timings else 0
        refinement_ocr_calls = int(extraction.refinement_regions)
        extraction.timings.setdefault("primary_tile_count", float(primary_ocr_calls))
        extraction.timings["primary_ocr_calls"] = float(primary_ocr_calls)
        extraction.timings["secondary_full_ocr_calls"] = float(secondary_full_ocr_calls)
        extraction.timings["refinement_ocr_calls"] = float(refinement_ocr_calls)
        extraction.timings["ocr_call_count"] = float(
            primary_ocr_calls + secondary_full_ocr_calls + refinement_ocr_calls
        )
        if recognition_region.enabled:
            recognition_mode = "fixed_document_region"
        else:
            recognition_mode = (
                "tiled_full_page"
                if extraction.timings.get("primary_tile_count", 0.0) > 1.0
                else "full_page"
            )
        return _result_payload(
            job,
            extraction,
            args.ocr_rotation,
            args.ocr_document_long_side,
            ocr_image_sha256=image_sha256,
            recognition_mode=recognition_mode,
            recognition_region=recognition_region.to_dict(),
        )

    try:
        while True:
            frame = source.read_new()
            if frame is None:
                time.sleep(args.poll_interval)
                continue
            now = time.monotonic()
            detection = detector.detect_jpeg(frame.image_bytes)
            observation = None
            if detection.detected:
                observation = PaperObservation.from_corners(
                    now,
                    detection.corners,
                    detection.frame_width,
                    detection.frame_height,
                    detection.confidence,
                )
            update = tracker.update(observation, timestamp=now)
            processed += 1

            if update.reason in {"paper_removed", "paper_not_detected", "paper_acquired"}:
                if update.reason != "paper_acquired":
                    capture_id = None
                burst_selector = None
                burst_status = None
                completed = None
                worker.discard_pending_except(capture_id)

            if update.triggered:
                capture_id = secrets.token_hex(16)
                burst_selector = BurstQualitySelector(
                    detection,
                    target_frames=args.burst_frames,
                    min_sharpness=args.minimum_sharpness,
                    max_glare_ratio=args.maximum_glare_ratio,
                )
                burst_status = None
                completed = None

            if burst_selector is not None and capture_id is not None and not update.triggered:
                selection = burst_selector.add_frame(frame.image_bytes, detection, now)
                if selection is not None:
                    burst_status = selection.public_status()
                    job = TextCaptureJob(capture_id, selection, time.time())
                    burst_selector = None
                    if selection.accepted:
                        set_progress(capture_id, "ocr_primary", {})
                        worker.submit(capture_id, lambda current=job: execute(current))
                    else:
                        completed = _failure_payload(job, "rejected", "burst_quality")
                        write_result(args.full_text_result_file, completed)

            finished = worker.poll()
            if finished is not None:
                finished_id, payload, error = finished
                with progress_lock:
                    progress.pop(finished_id, None)
                if finished_id == capture_id:
                    if error is not None:
                        completed = {
                            "status": "error",
                            "capture_id": finished_id,
                            "created_at": time.time(),
                            "source": {},
                            "quality": {},
                            "timings": {},
                            "reasons": ["ocr_runtime:%s" % type(error).__name__],
                            "document": None,
                        }
                    else:
                        completed = payload
                    write_result(args.full_text_result_file, completed)
                    result_count += 1
                    print(
                        json.dumps(
                            {
                                "event": "ocr_completed",
                                "capture": finished_id[:8],
                                "status": completed.get("status"),
                                "items": ((completed.get("document") or {}).get("item_count", 0)),
                                "mean_confidence": ((completed.get("document") or {}).get("mean_confidence", 0.0)),
                                "total_ms": ((completed.get("timings") or {}).get("total_ms")),
                            },
                            ensure_ascii=True,
                        ),
                        flush=True,
                    )

            status = _paper_status(detection, update)
            status.update(
                {
                    "event": "frame",
                    "updated_at": time.time(),
                    "stable_target_seconds": args.stable_seconds,
                    "burst_target_frames": args.burst_frames,
                    "ocr_document_long_side": args.ocr_document_long_side,
                    "ocr_rotation": args.ocr_rotation,
                    "recognition_region": recognition_region.to_dict(),
                    "processed_frames": processed,
                    "capture_id": capture_id,
                    "text_only": True,
                    "field_a": None,
                    "field_b": None,
                    "verification": {},
                    "burst_a": None,
                    "burst_b": None,
                    "burst": burst_selector.public_status() if burst_selector is not None else burst_status,
                }
            )
            if completed is not None and completed.get("capture_id") == capture_id:
                document = completed.get("document") or {}
                status["capture_stage"] = "completed"
                status["ocr_available"] = True
                status["text_detected"] = bool(document.get("blocks"))
                status["full_text"] = {
                    "available": bool(document.get("blocks")),
                    "status": completed.get("status"),
                    "line_count": int(document.get("line_count") or 0),
                    "item_count": int(document.get("item_count") or 0),
                    "mean_confidence": float(document.get("mean_confidence") or 0.0),
                    "elapsed_ms": float((completed.get("timings") or {}).get("total_ms") or 0.0),
                    "reasons": list(completed.get("reasons") or []),
                }
            elif burst_selector is not None:
                status["capture_stage"] = "collecting_frames"
                status["ocr_available"] = False
                status["text_detected"] = False
                status["full_text"] = None
            else:
                job_stage = worker.stage_for(capture_id)
                if job_stage is not None:
                    with progress_lock:
                        current_progress = dict(progress.get(capture_id or "", {}))
                    status["capture_stage"] = str(current_progress.get("stage") or ("queued" if job_stage == "queued" else "ocr_primary"))
                    status["ocr_available"] = False
                    status["text_detected"] = False
                    status["full_text"] = None
                else:
                    status["capture_stage"] = update.state.value
                    status["ocr_available"] = False
                    status["text_detected"] = False
                    status["full_text"] = None
            write_status(args.status_file, status)

            signature = (
                status["state"],
                status["reason"],
                status["capture_stage"],
                (status.get("burst") or {}).get("collected_frames"),
            )
            if signature != last_signature:
                print(
                    json.dumps(
                        {
                            "event": "state",
                            "capture": (capture_id or "")[:8],
                            "state": status["state"],
                            "stage": status["capture_stage"],
                        },
                        ensure_ascii=True,
                    ),
                    flush=True,
                )
                last_signature = signature
            if args.once_after_result and result_count:
                break
            if args.max_frames and processed >= args.max_frames:
                break
    except KeyboardInterrupt:
        pass
    finally:
        worker.close()
    return 0


def _write_configuration_image(
    directory: Any,
    capture_id: str,
    image_bytes: bytes,
    retain_count: int = 12,
) -> None:
    root = directory if hasattr(directory, "mkdir") else Path(str(directory))
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    destination = root / (capture_id + ".jpg")
    temporary = root / (".%s.%d.tmp" % (capture_id, os.getpid()))
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(image_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    images = sorted(root.glob("*.jpg"), key=lambda path: path.stat().st_mtime, reverse=True)
    for stale in images[max(1, retain_count):]:
        try:
            stale.unlink()
        except OSError:
            pass
