#!/usr/bin/env python3
"""Run the lightweight DocAligner paper-presence and four-corner probe.

This is intentionally an offline proof of concept.  It does not start a
camera, call OCR, or modify any gateway service.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Detect a visible document and its four corners with DocAligner."
    )
    parser.add_argument("--image", required=True, type=Path, help="JPEG or PNG image")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output") / "docaligner",
        help="Directory for the local diagnostic JSON and annotated image",
    )
    parser.add_argument(
        "--turbojpeg-bin",
        type=Path,
        default=None,
        help="Directory containing turbojpeg.dll (Windows only, if it is not on PATH)",
    )
    parser.add_argument(
        "--warmup-runs",
        type=int,
        default=4,
        help="Extra inference runs used only to measure warm performance",
    )
    return parser.parse_args()


def add_turbojpeg_path(directory: Optional[Path]) -> None:
    if directory is None:
        return
    resolved = directory.resolve()
    if not (resolved / "turbojpeg.dll").is_file():
        raise FileNotFoundError(f"turbojpeg.dll not found in: {resolved}")
    os.environ["PATH"] = f"{resolved}{os.pathsep}{os.environ.get('PATH', '')}"


def read_image(path: Path, cv2: Any, np: Any) -> Any:
    # cv2.imread cannot reliably open non-ASCII Windows paths.
    raw = np.fromfile(path, dtype=np.uint8)
    image = cv2.imdecode(raw, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"Cannot decode image: {path}")
    return image


def build_result(
    image: Any,
    points: Any,
    load_ms: float,
    timings_ms: list[float],
    cv2: Any,
) -> dict[str, Any]:
    height, width = image.shape[:2]
    point_list = [[round(float(x), 2), round(float(y), 2)] for x, y in points.tolist()]
    detected = len(point_list) == 4
    result: dict[str, Any] = {
        "detected": detected,
        "image_size": {"width": int(width), "height": int(height)},
        "corners": point_list,
        "model_load_ms": round(load_ms, 2),
        "first_inference_ms": round(timings_ms[0], 2),
        "warm_inference_avg_ms": round(sum(timings_ms[1:]) / max(1, len(timings_ms) - 1), 2),
    }
    if not detected:
        result["reason"] = "DocAligner did not find one complete visible document."
        return result

    contour = points.astype("float32")
    area = abs(float(cv2.contourArea(contour)))
    xs = [point[0] for point in point_list]
    ys = [point[1] for point in point_list]
    result["bbox"] = {
        "left": round(min(xs), 2),
        "top": round(min(ys), 2),
        "right": round(max(xs), 2),
        "bottom": round(max(ys), 2),
    }
    result["document_area_ratio"] = round(area / (width * height), 4)
    return result


def draw_annotation(image: Any, result: dict[str, Any], cv2: Any, np: Any) -> Any:
    rendered = image.copy()
    color = (49, 186, 77) if result["detected"] else (40, 50, 235)
    label = "document detected" if result["detected"] else "no complete document"
    if result["detected"]:
        polygon = np.array(result["corners"], dtype=np.int32).reshape((-1, 1, 2))
        cv2.polylines(rendered, [polygon], True, color, 4, cv2.LINE_AA)
        for index, point in enumerate(result["corners"], start=1):
            center = (round(point[0]), round(point[1]))
            cv2.circle(rendered, center, 8, color, -1, cv2.LINE_AA)
            cv2.putText(rendered, str(index), center, cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2, cv2.LINE_AA)
    cv2.rectangle(rendered, (16, 16), (370, 60), (255, 255, 255), -1)
    cv2.putText(rendered, label, (28, 48), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2, cv2.LINE_AA)
    return rendered


def main() -> int:
    args = parse_args()
    if args.warmup_runs < 0:
        raise ValueError("--warmup-runs must be zero or greater")
    if not args.image.is_file():
        raise FileNotFoundError(f"Image not found: {args.image}")

    add_turbojpeg_path(args.turbojpeg_bin)
    import cv2
    import numpy as np
    from docaligner import DocAligner, ModelType

    image = read_image(args.image, cv2, np)
    load_started = time.perf_counter()
    model = DocAligner(model_type=ModelType.point, model_cfg="lcnet050")
    load_ms = (time.perf_counter() - load_started) * 1000

    timings_ms: list[float] = []
    points = None
    for _ in range(args.warmup_runs + 1):
        started = time.perf_counter()
        points = model(image)
        timings_ms.append((time.perf_counter() - started) * 1000)
    assert points is not None

    result = build_result(image, points, load_ms, timings_ms, cv2)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = args.image.stem
    json_path = args.output_dir / f"{stem}.docaligner.json"
    image_path = args.output_dir / f"{stem}.docaligner.jpg"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    ok, encoded = cv2.imencode(".jpg", draw_annotation(image, result, cv2, np))
    if not ok:
        raise RuntimeError("Could not encode diagnostic image")
    encoded.tofile(image_path)

    print(json.dumps({**result, "json": str(json_path), "annotated_image": str(image_path)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"DocAligner probe failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
