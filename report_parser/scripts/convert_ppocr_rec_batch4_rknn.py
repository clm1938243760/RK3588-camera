#!/usr/bin/env python3
"""Convert the validated static PP-OCRv4 batch recognizer to an RKNN artifact.

Run this script only in the pinned RKNN Toolkit2 environment.  It deliberately
uses floating-point input and disables quantization because preprocessing is
performed by the native OCR worker before calling the NPU.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from rknn.api import RKNN


INPUT_NAME = "x"
OUTPUT_NAME = "softmax_11.tmp_0"
INPUT_SHAPE = [4, 3, 48, 320]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_ok(operation: str, code: int) -> None:
    if code != 0:
        raise RuntimeError(f"RKNN {operation} failed: {code}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    if not args.model.is_file():
        parser.error(f"ONNX model does not exist: {args.model}")
    if args.output.exists():
        parser.error(f"refusing to overwrite existing output: {args.output}")
    args.output.parent.mkdir(parents=True, exist_ok=True)

    rknn = RKNN(verbose=True)
    try:
        _require_ok(
            "config",
            rknn.config(
                target_platform="rk3588",
                mean_values=[[0.0, 0.0, 0.0]],
                std_values=[[1.0, 1.0, 1.0]],
            ),
        )
        _require_ok(
            "load_onnx",
            rknn.load_onnx(
                model=str(args.model),
                inputs=[INPUT_NAME],
                input_size_list=[INPUT_SHAPE],
                outputs=[OUTPUT_NAME],
            ),
        )
        _require_ok("build", rknn.build(do_quantization=False))
        _require_ok("export_rknn", rknn.export_rknn(str(args.output)))
    finally:
        rknn.release()

    print(json.dumps({
        "status": "built",
        "target_platform": "rk3588",
        "quantization": "disabled",
        "input_name": INPUT_NAME,
        "input_shape": INPUT_SHAPE,
        "output_name": OUTPUT_NAME,
        "source_sha256": _sha256(args.model),
        "rknn_sha256": _sha256(args.output),
        "rknn_size": args.output.stat().st_size,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
