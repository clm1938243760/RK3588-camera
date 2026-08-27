from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .uie_extraction import (
    PaddleTaskflowEngine,
    blocks_from_payload,
    load_uie_schema,
    run_uie_extraction,
    uie_prompts,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate a PaddleNLP UIE text model against stored OCR evidence"
    )
    parser.add_argument("--ocr-json", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--schema", type=Path)
    parser.add_argument(
        "--model",
        choices=("uie-base", "uie-medical-base"),
        default="uie-base",
    )
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
        engine = PaddleTaskflowEngine(
            args.model,
            uie_prompts(schema),
            device=args.device,
            position_prob=args.position_prob,
            max_seq_len=args.max_seq_len,
        )
        result = run_uie_extraction(
            blocks_from_payload(payload), schema, args.model, engine.predict
        )
    except Exception as exc:
        result = {
            "schema_version": 1,
            "status": "error",
            "model": args.model,
            "error": type(exc).__name__,
        }
    serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        sys.stdout.write(serialized)
    else:
        _atomic_write(args.output, serialized)
    return 0 if result.get("status") == "accepted" else 1 if result.get("status") != "error" else 2


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
