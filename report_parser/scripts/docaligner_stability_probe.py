#!/usr/bin/env python3
"""Feed still images through DocAligner and the paper stability tracker."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, List


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate paper acquisition, stability, and one-shot triggering."
    )
    parser.add_argument("--images", nargs="+", required=True, type=Path)
    parser.add_argument(
        "--frame-interval",
        type=float,
        default=0.25,
        help="Synthetic interval between frames; 0.25 seconds represents 4 FPS",
    )
    parser.add_argument(
        "--repeat-each",
        type=int,
        default=1,
        help="Repeat each image this many times before advancing to the next image",
    )
    parser.add_argument("--stable-seconds", type=float, default=0.8)
    parser.add_argument("--output", type=Path, default=Path("output") / "docaligner_sequence.json")
    parser.add_argument("--turbojpeg-bin", type=Path, default=None)
    return parser.parse_args()


def add_turbojpeg_path(directory: Path) -> None:
    resolved = directory.resolve()
    if not (resolved / "turbojpeg.dll").is_file():
        raise FileNotFoundError(f"turbojpeg.dll not found in: {resolved}")
    os.environ["PATH"] = f"{resolved}{os.pathsep}{os.environ.get('PATH', '')}"


def read_image(path: Path, cv2: Any, np: Any) -> Any:
    raw = np.fromfile(path, dtype=np.uint8)
    image = cv2.imdecode(raw, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"Cannot decode image: {path}")
    return image


def main() -> int:
    args = parse_args()
    if args.frame_interval <= 0:
        raise ValueError("--frame-interval must be greater than zero")
    if args.repeat_each < 1:
        raise ValueError("--repeat-each must be at least one")
    for path in args.images:
        if not path.is_file():
            raise FileNotFoundError(f"Image not found: {path}")
    if args.turbojpeg_bin is not None:
        add_turbojpeg_path(args.turbojpeg_bin)

    import cv2
    import numpy as np
    from docaligner import DocAligner, ModelType
    from rk3588_report_parser.paper_trigger import (
        PaperObservation,
        PaperStabilityTracker,
        PaperTrackerConfig,
    )

    model_started = time.perf_counter()
    model = DocAligner(model_type=ModelType.point, model_cfg="lcnet050")
    model_load_ms = (time.perf_counter() - model_started) * 1000
    tracker = PaperStabilityTracker(
        PaperTrackerConfig(stable_seconds=args.stable_seconds)
    )

    expanded_images: List[Path] = []
    for path in args.images:
        expanded_images.extend([path] * args.repeat_each)

    frames = []
    trigger_frames = []
    for index, path in enumerate(expanded_images):
        timestamp = index * args.frame_interval
        image = read_image(path, cv2, np)
        started = time.perf_counter()
        corners = model(image)
        inference_ms = (time.perf_counter() - started) * 1000
        observation = None
        if len(corners) == 4:
            height, width = image.shape[:2]
            observation = PaperObservation.from_corners(
                timestamp=timestamp,
                corners=corners,
                frame_width=width,
                frame_height=height,
            )
        update = tracker.update(observation, timestamp=timestamp)
        if update.triggered:
            trigger_frames.append(index)
        frames.append(
            {
                "index": index,
                "timestamp": round(timestamp, 3),
                "image": path.name,
                "detected": observation is not None,
                "state": update.state.value,
                "triggered": update.triggered,
                "reason": update.reason,
                "stable_for": round(update.stable_for, 3),
                "observations": update.observations,
                "iou": None if update.iou is None else round(update.iou, 4),
                "inference_ms": round(inference_ms, 2),
            }
        )

    result = {
        "model": "DocAligner point/lcnet050",
        "model_load_ms": round(model_load_ms, 2),
        "frame_interval": args.frame_interval,
        "stable_seconds": args.stable_seconds,
        "frame_count": len(frames),
        "trigger_count": len(trigger_frames),
        "trigger_frames": trigger_frames,
        "frames": frames,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"DocAligner stability probe failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
