#!/usr/bin/env python3
"""Build a static batch PP-OCRv4 recognition ONNX candidate for RKNN.

The production recognizer runs a dynamic batch-one PP-OCRv4 model.  RKNN
conversion requires a static shape, so this script keeps the network weights
unchanged and fixes only the public input/output tensor dimensions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import onnx


INPUT_NAME = "x"
OUTPUT_NAME = "softmax_11.tmp_0"
CHANNELS = 3
HEIGHT = 48
CLASSES = 6625


def _set_dim(dim: onnx.TensorShapeProto.Dimension, value: int) -> None:
    dim.ClearField("dim_param")
    dim.dim_value = value


def _shape(value_info: onnx.ValueInfoProto) -> list[int | str]:
    return [
        dim.dim_value if dim.HasField("dim_value") else dim.dim_param
        for dim in value_info.type.tensor_type.shape.dim
    ]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(source: Path, output: Path, batch_size: int, width: int) -> dict[str, object]:
    model = onnx.load_model(source)
    inputs = {value.name: value for value in model.graph.input}
    outputs = {value.name: value for value in model.graph.output}
    if set(inputs) != {INPUT_NAME}:
        raise ValueError(f"unexpected model inputs: {sorted(inputs)}")
    if set(outputs) != {OUTPUT_NAME}:
        raise ValueError(f"unexpected model outputs: {sorted(outputs)}")

    input_tensor = inputs[INPUT_NAME]
    input_dims = input_tensor.type.tensor_type.shape.dim
    if len(input_dims) != 4:
        raise ValueError(f"expected NCHW input, got {_shape(input_tensor)}")
    _set_dim(input_dims[0], batch_size)
    _set_dim(input_dims[1], CHANNELS)
    _set_dim(input_dims[2], HEIGHT)
    _set_dim(input_dims[3], width)

    inferred = onnx.shape_inference.infer_shapes(model)
    output_tensor = {value.name: value for value in inferred.graph.output}[OUTPUT_NAME]
    output_dims = output_tensor.type.tensor_type.shape.dim
    if len(output_dims) != 3:
        raise ValueError(f"expected [batch,time,class] output, got {_shape(output_tensor)}")

    # PP-OCRv4 recognition reduces the horizontal dimension by a factor of 8.
    expected_time_steps = width // 8
    _set_dim(output_dims[0], batch_size)
    _set_dim(output_dims[1], expected_time_steps)
    _set_dim(output_dims[2], CLASSES)

    onnx.checker.check_model(inferred)
    output.parent.mkdir(parents=True, exist_ok=True)
    onnx.save_model(inferred, output)

    return {
        "source": str(source),
        "source_sha256": _sha256(source),
        "output": str(output),
        "output_sha256": _sha256(output),
        "input": _shape(input_tensor),
        "output_shape": _shape(output_tensor),
        "batch_size": batch_size,
        "width": width,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--width", type=int, default=320)
    args = parser.parse_args()

    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    if args.width < 8 or args.width % 8:
        parser.error("--width must be a positive multiple of 8")
    if not args.source.is_file():
        parser.error(f"source ONNX does not exist: {args.source}")

    print(json.dumps(build(args.source, args.output, args.batch_size, args.width), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
