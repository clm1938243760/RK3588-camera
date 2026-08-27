#!/usr/bin/env python3
"""Verify that static batch recognition preserves per-sample ONNX output."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import onnxruntime as ort


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(session: ort.InferenceSession, image: np.ndarray) -> np.ndarray:
    return session.run(None, {session.get_inputs()[0].name: image})[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--batch-model", required=True, type=Path)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--width", type=int, default=320)
    parser.add_argument("--seed", type=int, default=20260824)
    args = parser.parse_args()

    source = ort.InferenceSession(str(args.source), providers=["CPUExecutionProvider"])
    batch = ort.InferenceSession(str(args.batch_model), providers=["CPUExecutionProvider"])
    rng = np.random.default_rng(args.seed)
    images = rng.uniform(-1.0, 1.0, size=(args.batch_size, 3, 48, args.width)).astype(np.float32)

    serial = np.concatenate([_run(source, image[None, ...]) for image in images], axis=0)
    batched = _run(batch, images)
    declared_output = [
        dimension if isinstance(dimension, int) else None
        for dimension in batch.get_outputs()[0].shape
    ]
    if serial.shape != batched.shape:
        raise SystemExit(f"output shape differs: serial={serial.shape}, batch={batched.shape}")
    if tuple(declared_output) != batched.shape:
        raise SystemExit(
            f"batch model declares {declared_output}, but returns {list(batched.shape)}"
        )

    max_abs_diff = float(np.max(np.abs(serial - batched)))
    if not np.allclose(serial, batched, rtol=1e-5, atol=1e-6):
        raise SystemExit(f"batch output differs from serial output: max_abs_diff={max_abs_diff}")

    print(json.dumps({
        "status": "passed",
        "source_sha256": _sha256(args.source),
        "batch_model_sha256": _sha256(args.batch_model),
        "input_shape": list(images.shape),
        "declared_output_shape": declared_output,
        "output_shape": list(batched.shape),
        "max_abs_diff": max_abs_diff,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
