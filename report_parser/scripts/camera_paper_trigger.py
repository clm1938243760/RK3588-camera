#!/usr/bin/env python3
"""Watch camera snapshots, detect a stable paper, and call OCR once."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Low-frequency DocAligner paper trigger for the RK3588 CSI camera"
    )
    parser.add_argument("--frame-glob", default="/tmp/rk3588_camera_ocr_*.jpg")
    parser.add_argument(
        "--paper-model",
        type=Path,
        default=PROJECT_ROOT / "runtime" / "docaligner" / "lcnet050_p_multi_decoder_l3_d64_256_fp32.onnx",
    )
    parser.add_argument("--object-threshold", type=float, default=0.5)
    parser.add_argument(
        "--detector-backend",
        choices=("auto", "opencv", "onnxruntime"),
        default="auto",
    )
    parser.add_argument("--stable-seconds", type=float, default=0.5)
    parser.add_argument("--min-observations", type=int, default=3)
    parser.add_argument("--remove-seconds", type=float, default=0.5)
    parser.add_argument("--poll-interval", type=float, default=0.05)
    parser.add_argument("--ocr-endpoint", default="http://127.0.0.1:5002/ocr")
    parser.add_argument("--ocr-timeout", type=float, default=30.0)
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "config.rk3588.ocr_only.json",
    )
    parser.add_argument(
        "--rules-file",
        type=Path,
        default=PROJECT_ROOT / "runtime" / "active_identifier_rules.json",
    )
    parser.add_argument("--burst-frames", type=int, default=2)
    parser.add_argument("--minimum-sharpness", type=float, default=20.0)
    parser.add_argument("--maximum-glare-ratio", type=float, default=0.85)
    parser.add_argument("--second-pass-delay", type=float, default=0.2)
    parser.add_argument("--retry-delay", type=float, default=0.5)
    parser.add_argument("--max-compare-attempts", type=int, default=3)
    parser.add_argument(
        "--ocr-rotation",
        type=int,
        choices=(0, 90, 180, 270),
        default=0,
        help="counterclockwise JPEG rotation applied only before OCR",
    )
    parser.add_argument(
        "--ocr-document-long-side",
        type=int,
        default=3200,
        help="target long-side pixels for the rectified image sent to OCR",
    )
    parser.add_argument("--status-file", type=Path)
    parser.add_argument("--result-file", type=Path)
    parser.add_argument("--full-text-result-file", type=Path)
    parser.add_argument(
        "--configuration-image-dir",
        type=Path,
        default=Path("/run/rk3588-report-parser/configuration-images"),
    )
    parser.add_argument("--text-only", action="store_true")
    parser.add_argument("--ocr-total-budget", type=float, default=10.0)
    parser.add_argument(
        "--ocr-tile-max-aspect",
        type=float,
        default=0.0,
        help="split long rectified documents into overlapping near-square OCR tiles when greater than this aspect ratio; 0 disables",
    )
    parser.add_argument("--ocr-tile-overlap-ratio", type=float, default=0.15)
    parser.add_argument("--ocr-tile-max-count", type=int, default=4)
    parser.add_argument(
        "--ocr-refinement-max-regions",
        type=int,
        default=3,
        help="maximum low-confidence regions to OCR again; 0 disables regional refinement",
    )
    parser.add_argument(
        "--ocr-region-crop-top",
        type=float,
        default=0.0,
        help="normalized top edge of the document crop sent to OCR",
    )
    parser.add_argument(
        "--ocr-region-crop-bottom",
        type=float,
        default=1.0,
        help="normalized bottom edge of the document crop sent to OCR",
    )
    parser.add_argument(
        "--ocr-region-accept-top",
        type=float,
        default=0.0,
        help="normalized top edge whose OCR block centers are retained",
    )
    parser.add_argument(
        "--ocr-region-accept-bottom",
        type=float,
        default=1.0,
        help="normalized bottom edge whose OCR block centers are retained",
    )
    parser.add_argument("--max-frames", type=int, default=0)
    parser.add_argument("--once-after-ocr", action="store_true")
    parser.add_argument("--once-after-burst", action="store_true")
    parser.add_argument("--once-after-field-a", action="store_true")
    parser.add_argument("--once-after-verification", action="store_true")
    parser.add_argument("--once-after-result", action="store_true")
    return parser.parse_args()


def require_loopback(endpoint: str) -> None:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("OCR endpoint must be loopback-only")


def write_status(path: Optional[Path], payload: Dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def write_result(path: Optional[Path], payload: Dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
            handle.write("\n")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def main() -> int:
    args = parse_args()
    if args.poll_interval <= 0:
        raise ValueError("--poll-interval must be greater than zero")
    if args.max_frames < 0:
        raise ValueError("--max-frames must be zero or greater")
    if args.burst_frames < 2:
        raise ValueError("--burst-frames must be at least two")
    if args.ocr_document_long_side < 640:
        raise ValueError("--ocr-document-long-side must be at least 640")
    if args.second_pass_delay < 0:
        raise ValueError("--second-pass-delay must be zero or greater")
    if args.retry_delay < 0:
        raise ValueError("--retry-delay must be zero or greater")
    if args.max_compare_attempts < 1:
        raise ValueError("--max-compare-attempts must be at least one")
    if args.ocr_total_budget < 2:
        raise ValueError("--ocr-total-budget must be at least two seconds")
    if args.ocr_tile_max_aspect != 0 and args.ocr_tile_max_aspect <= 1.0:
        raise ValueError("--ocr-tile-max-aspect must be zero or greater than one")
    if not 0 <= args.ocr_tile_overlap_ratio <= 0.45:
        raise ValueError("--ocr-tile-overlap-ratio must be between zero and 0.45")
    if not 2 <= args.ocr_tile_max_count <= 8:
        raise ValueError("--ocr-tile-max-count must be between two and eight")
    if not 0 <= args.ocr_refinement_max_regions <= 8:
        raise ValueError("--ocr-refinement-max-regions must be between zero and eight")
    if not (
        0.0 <= args.ocr_region_crop_top <= args.ocr_region_accept_top
        < args.ocr_region_accept_bottom <= args.ocr_region_crop_bottom <= 1.0
    ):
        raise ValueError(
            "OCR accepted vertical region must be contained by the crop region"
        )
    require_loopback(args.ocr_endpoint)

    if args.text_only:
        from rk3588_report_parser.capture_text_runtime import run_text_only

        return run_text_only(args, write_status, write_result)

    from rk3588_report_parser.capture_trigger import TriggeredOcrController
    from rk3588_report_parser.capture_identifier import (
        create_capture_identifier_extractor,
        decide_capture_retry,
        load_capture_parser_settings,
        verify_capture_pair,
    )
    from rk3588_report_parser.capture_orientation import prepare_document_jpeg
    from rk3588_report_parser.capture_text import FullTextExtractor
    from rk3588_report_parser.clients import LocalPpOcrClient
    from rk3588_report_parser.frame_source import LatestJpegFrameSource
    from rk3588_report_parser.frame_quality import BurstQualitySelector
    from rk3588_report_parser.paper_detector import create_docaligner_detector
    from rk3588_report_parser.paper_trigger import PaperStabilityTracker, PaperTrackerConfig

    detector = create_docaligner_detector(
        args.paper_model,
        args.object_threshold,
        args.detector_backend,
    )
    source = LatestJpegFrameSource(args.frame_glob)
    ocr_client = LocalPpOcrClient()
    capture_settings = load_capture_parser_settings(
        args.config,
        args.rules_file,
        args.ocr_endpoint,
        args.ocr_timeout,
    )
    ocr_settings = capture_settings.ocr
    identifier_extractor = create_capture_identifier_extractor(
        capture_settings,
        ocr_client=ocr_client,
    )
    full_text_extractor = FullTextExtractor(ocr_client, ocr_settings)

    def prepare_for_ocr(image_bytes: bytes, detection: Any) -> bytes:
        return prepare_document_jpeg(
            image_bytes,
            detection,
            degrees_counterclockwise=args.ocr_rotation,
            target_long_side=args.ocr_document_long_side,
        )

    controller = TriggeredOcrController(
        detector=detector,
        tracker=PaperStabilityTracker(
            PaperTrackerConfig(
                stable_seconds=args.stable_seconds,
                min_observations=args.min_observations,
                remove_seconds=args.remove_seconds,
            )
        ),
        ocr_callback=lambda image: ocr_client.recognize(image, ocr_settings),
        ocr_image_preprocessor=prepare_for_ocr,
    )

    print(
        json.dumps(
            {
                "event": "started",
                "frame_glob": args.frame_glob,
                "model_load_ms": round(detector.model_load_ms, 2),
                "detector_backend": detector.backend_name,
                "identifier_rule_profile": capture_settings.identifier_rules.profile,
                "burst_frames": args.burst_frames,
                "ocr_document_long_side": args.ocr_document_long_side,
                "patient_images_saved": False,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    processed = 0
    last_signature = None
    burst_selector = None
    burst_phase = None
    burst_status = None
    burst_a_status = None
    burst_b_status = None
    selected_frame = None
    field_a = None
    field_b = None
    document_a = None
    document_source = None
    full_text_status = None
    capture_id = None
    next_capture_not_before = None
    compare_attempt = 0
    verification = None
    result_written = False
    try:
        while True:
            frame = source.read_new()
            if frame is None:
                time.sleep(args.poll_interval)
                continue
            result = controller.process_frame(frame.image_bytes, time.monotonic())
            processed += 1
            if result.tracker.reason in {"paper_removed", "paper_not_detected", "paper_acquired"}:
                burst_selector = None
                burst_phase = None
                burst_status = None
                burst_a_status = None
                burst_b_status = None
                selected_frame = None
                field_a = None
                field_b = None
                document_a = None
                document_source = None
                full_text_status = None
                capture_id = None
                next_capture_not_before = None
                compare_attempt = 0
                verification = None
                result_written = False
            if result.ocr_attempted:
                if result.text_detected and result.ocr_error is None:
                    compare_attempt = 1
                    burst_selector = BurstQualitySelector(
                        reference=result.detection,
                        target_frames=args.burst_frames,
                        min_sharpness=args.minimum_sharpness,
                        max_glare_ratio=args.maximum_glare_ratio,
                    )
                    burst_phase = "a"
                    burst_status = None
                    burst_a_status = None
                    burst_b_status = None
                    selected_frame = None
                    field_a = None
                    field_b = None
                    document_a = None
                    document_source = None
                    full_text_status = None
                    capture_id = secrets.token_hex(16)
                    next_capture_not_before = None
                    verification = None
                    result_written = False
            elif result.ocr_available:
                current_time = time.monotonic()
                if (
                    burst_selector is None
                    and next_capture_not_before is not None
                    and current_time >= next_capture_not_before
                    and result.detection.detected
                ):
                    burst_phase = "b" if field_a is not None and field_a.accepted else "a"
                    burst_selector = BurstQualitySelector(
                        reference=result.detection,
                        target_frames=args.burst_frames,
                        min_sharpness=args.minimum_sharpness,
                        max_glare_ratio=args.maximum_glare_ratio,
                    )
                    burst_status = None
                    if burst_phase == "a":
                        burst_a_status = None
                    else:
                        burst_b_status = None
                    next_capture_not_before = None
                if burst_selector is None:
                    selection = None
                else:
                    selection = burst_selector.add_frame(
                        frame.image_bytes,
                        result.detection,
                        current_time,
                    )
                if selection is not None:
                    completed_phase = burst_phase
                    completed_selector = burst_selector
                    burst_status = selection.public_status()
                    burst_status["rejected_frames"] = completed_selector.rejected_frames
                    burst_status["quality_failures"] = completed_selector.quality_failures
                    if completed_phase == "a":
                        burst_a_status = burst_status
                    else:
                        burst_b_status = burst_status
                    selected_frame = selection.best_frame if selection.accepted else None
                    burst_selector = None
                    burst_phase = None

                    if not selection.accepted:
                        verification = {
                            "status": "rejected",
                            "reason": "%s_burst_quality" % completed_phase,
                            "attempt": compare_attempt,
                        }
                    elif args.once_after_burst:
                        pass
                    elif completed_phase == "a":
                        current_document_source = {
                            "frame_size": {
                                "width": selected_frame.detection.frame_width,
                                "height": selected_frame.detection.frame_height,
                            },
                            "paper_corners": [
                                [round(float(x), 3), round(float(y), 3)]
                                for x, y in selected_frame.detection.corners
                            ],
                            "ocr_rotation": args.ocr_rotation,
                            "ocr_document_long_side": args.ocr_document_long_side,
                        }
                        prepared_image = prepare_for_ocr(
                            selected_frame.image_bytes,
                            selected_frame.detection,
                        )
                        if document_a is None:
                            full_text_extraction = full_text_extractor.extract(prepared_image)
                            full_text_status = full_text_extraction.public_status()
                            if full_text_extraction.accepted:
                                document_a = full_text_extraction.document
                                document_source = current_document_source
                                if args.full_text_result_file is not None and capture_id is not None:
                                    write_result(
                                        args.full_text_result_file,
                                        {
                                            "status": "accepted",
                                            "capture_id": capture_id,
                                            "created_at": time.time(),
                                            "source": document_source,
                                            "document": document_a.to_dict(),
                                        },
                                    )
                        field_a = identifier_extractor.extract(
                            prepared_image
                        )
                        selected_frame = None
                        if field_a.accepted:
                            next_capture_not_before = time.monotonic() + args.second_pass_delay
                        else:
                            decision = decide_capture_retry(
                                "field_a_%s" % field_a.status,
                                compare_attempt,
                                args.max_compare_attempts,
                            )
                            verification = decision.public_status()
                            if decision.status == "retrying":
                                compare_attempt = decision.attempt
                                burst_a_status = None
                                field_a = None
                                next_capture_not_before = time.monotonic() + args.retry_delay
                    else:
                        field_b = identifier_extractor.extract(
                            prepare_for_ocr(selected_frame.image_bytes, selected_frame.detection)
                        )
                        selected_frame = None
                        decision = verify_capture_pair(
                            field_a,
                            field_b,
                            compare_attempt,
                            args.max_compare_attempts,
                        )
                        verification = decision.public_status()
                        if decision.status == "retrying":
                            compare_attempt = decision.attempt
                            burst_a_status = None
                            burst_b_status = None
                            field_a = None
                            field_b = None
                            next_capture_not_before = time.monotonic() + args.retry_delay

            status = result.public_status()
            status["event"] = "frame"
            status["updated_at"] = time.time()
            status["stable_target_seconds"] = args.stable_seconds
            status["burst_target_frames"] = args.burst_frames
            status["ocr_document_long_side"] = args.ocr_document_long_side
            status["processed_frames"] = processed
            status["capture_id"] = capture_id
            status["full_text"] = full_text_status
            status["field_a"] = None
            status["field_b"] = None
            status["verification"] = None
            status["burst_a"] = burst_a_status
            status["burst_b"] = burst_b_status
            status["best_frame_held_in_memory"] = False
            if verification is not None and verification["status"] in {"accepted", "rejected"}:
                status["capture_stage"] = "verified" if verification["status"] == "accepted" else "verification_rejected"
                status["burst"] = burst_status
                status["field_a"] = None if field_a is None else field_a.public_status()
                status["field_b"] = None if field_b is None else field_b.public_status()
                status["verification"] = dict(verification)
            elif burst_selector is not None:
                status["capture_stage"] = "collecting_%s" % burst_phase
                status["burst"] = burst_selector.public_status()
                status["field_a"] = None if field_a is None else field_a.public_status()
                status["field_b"] = None if field_b is None else field_b.public_status()
                status["verification"] = verification
            elif next_capture_not_before is not None:
                status["capture_stage"] = (
                    "waiting_b" if field_a is not None and field_a.accepted else "retry_waiting_a"
                )
                status["burst"] = burst_status
                status["field_a"] = None if field_a is None else field_a.public_status()
                status["field_b"] = None if field_b is None else field_b.public_status()
                status["verification"] = verification
            elif field_a is not None:
                status["capture_stage"] = (
                    "field_a_ready" if field_a.accepted else "field_a_" + field_a.status
                )
                status["burst"] = burst_status
                status["field_a"] = field_a.public_status()
                status["field_b"] = None if field_b is None else field_b.public_status()
                status["verification"] = verification
                status["best_frame_held_in_memory"] = False
            elif burst_status is not None:
                status["capture_stage"] = (
                    "burst_ready" if burst_status["accepted"] else "burst_rejected"
                )
                status["burst"] = burst_status
                status["best_frame_held_in_memory"] = selected_frame is not None
            elif result.ocr_available and result.ocr_error:
                status["capture_stage"] = "ocr_error"
                status["burst"] = None
                status["field_a"] = None
            elif result.ocr_available and not result.text_detected:
                status["capture_stage"] = "reposition_required"
                status["burst"] = None
                status["field_a"] = None
            else:
                status["capture_stage"] = result.tracker.state.value
                status["burst"] = None
                status["field_a"] = None
            write_status(args.status_file, status)

            if (
                verification is not None
                and verification["status"] == "accepted"
                and not result_written
                and field_a is not None
                and field_a.value is not None
            ):
                created_at = time.time()
                if capture_id is None:
                    capture_id = secrets.token_hex(16)
                write_result(
                    args.result_file,
                    {
                        "status": "accepted",
                        "identifier": field_a.value,
                        "verification": "two_pass_exact_match",
                        "attempt": compare_attempt,
                        "capture_id": capture_id,
                        "created_at": created_at,
                    },
                )
                result_written = True

            signature_burst = status.get("burst") or {}
            signature = (
                status["state"],
                status["reason"],
                status["ocr_attempted"],
                status["capture_stage"],
                signature_burst.get("collected_frames"),
                signature_burst.get("ready"),
            )
            if signature != last_signature or status["ocr_attempted"]:
                print(json.dumps(status, ensure_ascii=False), flush=True)
                last_signature = signature
            if args.once_after_ocr and result.ocr_attempted:
                break
            if args.once_after_burst and burst_status is not None:
                break
            if args.once_after_field_a and field_a is not None:
                break
            if (
                args.once_after_verification
                and verification is not None
                and verification["status"] in {"accepted", "rejected"}
            ):
                break
            if args.max_frames and processed >= args.max_frames:
                break
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("camera paper trigger failed: %s" % exc, file=sys.stderr)
        raise SystemExit(2)
