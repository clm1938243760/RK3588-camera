from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .uie_cli import _atomic_write
from .uie_extraction import (
    PaddleTaskflowXEngine,
    blocks_from_payload,
    load_uie_schema,
    run_uie_x_extraction,
    uie_prompts,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate UIE-X against a report image and stored OCR layout"
    )
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--ocr-json", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--device", choices=("cpu", "gpu"), default="cpu")
    parser.add_argument("--position-prob", type=float, default=0.5)
    parser.add_argument("--max-seq-len", type=int, default=512)
    return parser


def main(argv: Any = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with args.ocr_json.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        schema = load_uie_schema(args.schema)
        engine = PaddleTaskflowXEngine(
            uie_prompts(schema),
            device=args.device,
            position_prob=args.position_prob,
            max_seq_len=args.max_seq_len,
        )
        result = run_uie_x_extraction(
            blocks_from_payload(payload), schema, args.image, engine.predict
        )
    except Exception as exc:
        result = {
            "schema_version": 1,
            "status": "error",
            "model": "uie-x-base",
            "error": type(exc).__name__,
        }
    serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        sys.stdout.write(serialized)
    else:
        _atomic_write(args.output, serialized)
    return 0 if result.get("status") == "accepted" else 1 if result.get("status") != "error" else 2


if __name__ == "__main__":
    raise SystemExit(main())
