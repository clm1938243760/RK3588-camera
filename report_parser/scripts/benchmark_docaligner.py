#!/usr/bin/env python3
"""Benchmark the packaged DocAligner detector without retaining the image."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import mean
from typing import List

try:
    import resource
except ImportError:
    resource = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def percentile(values: List[float], fraction: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.5)))
    return ordered[index]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark DocAligner on one local JPEG")
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--backend", choices=("auto", "opencv", "onnxruntime"), default="auto")
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--iterations", type=int, default=100)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.warmup < 0 or args.iterations < 1:
        raise ValueError("warmup must be non-negative and iterations must be positive")

    from rk3588_report_parser.paper_detector import create_docaligner_detector

    image_bytes = args.image.read_bytes()
    detector = create_docaligner_detector(args.model, args.threshold, args.backend)
    for _ in range(args.warmup):
        detector.detect_jpeg(image_bytes)

    timings = []
    last_result = None
    for _ in range(args.iterations):
        last_result = detector.detect_jpeg(image_bytes)
        timings.append(last_result.inference_ms)

    import cv2
    import numpy

    try:
        import onnxruntime

        onnxruntime_version = onnxruntime.__version__
    except ImportError:
        onnxruntime_version = None

    average_ms = mean(timings)
    payload = {
        "backend": detector.backend_name,
        "intra_op_threads": getattr(detector, "intra_op_threads", None),
        "versions": {
            "python": "%d.%d.%d" % sys.version_info[:3],
            "opencv": cv2.__version__,
            "numpy": numpy.__version__,
            "onnxruntime": onnxruntime_version,
        },
        "model_load_ms": round(detector.model_load_ms, 3),
        "iterations": args.iterations,
        "warmup": args.warmup,
        "inference_ms": {
            "mean": round(average_ms, 3),
            "p50": round(percentile(timings, 0.50), 3),
            "p95": round(percentile(timings, 0.95), 3),
            "min": round(min(timings), 3),
            "max": round(max(timings), 3),
        },
        "theoretical_fps": round(1000.0 / average_ms, 2),
        "peak_rss_mb": (
            None
            if resource is None
            else round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 2)
        ),
        "detected": bool(last_result and last_result.detected),
        "confidence": None if last_result is None else round(last_result.confidence, 6),
        "image_retained": False,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("DocAligner benchmark failed: %s" % exc, file=sys.stderr)
        raise SystemExit(2)
