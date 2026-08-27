"""Convert the static text-only UIE encoder to an RK3588 FP16 RKNN model.

Run this script inside an x86 Linux environment with a matching RKNN Toolkit2.
The result must be loaded with a matching board-side RKNPU2 runtime before it
can be used by the camera service.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence


INPUT_NAMES = ["input_ids", "token_type_ids", "position_ids", "attention_mask"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convert UIE ONNX to RK3588 FP16 RKNN")
    parser.add_argument("--onnx", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sequence-length", type=int, choices=(128, 256, 512), default=256)
    parser.add_argument("--optimization-level", type=int, choices=(0, 1, 2, 3), default=1)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.onnx.is_file():
        raise SystemExit("ONNX model does not exist: %s" % args.onnx)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        from rknn.api import RKNN
    except ImportError as exc:
        raise SystemExit("rknn-toolkit2 must be installed in this Python environment") from exc

    rknn = RKNN(verbose=True)
    try:
        result = rknn.config(
            target_platform="rk3588",
            float_dtype="float16",
            optimization_level=args.optimization_level,
        )
        if result != 0:
            raise RuntimeError("RKNN config failed: %s" % result)
        shape = [1, args.sequence_length]
        result = rknn.load_onnx(
            model=str(args.onnx),
            inputs=INPUT_NAMES,
            input_size_list=[shape, shape, shape, shape],
        )
        if result != 0:
            raise RuntimeError("RKNN ONNX import failed: %s" % result)
        result = rknn.build(do_quantization=False)
        if result != 0:
            raise RuntimeError("RKNN FP16 build failed: %s" % result)
        result = rknn.export_rknn(str(args.output))
        if result != 0:
            raise RuntimeError("RKNN export failed: %s" % result)
    finally:
        rknn.release()
    print("RKNN model written: %s" % args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
