from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable, Optional

from .choice_linker import ConstrainedChoiceLinker
from .clients import ServiceError
from .evidence_chat_linker import EvidenceChatLinker
from .evidence_linker import EvidenceChoiceLinker
from .identifier_pipeline import IdentifierParser, write_identifier_debug
from .manifest import ManifestError, check_manifest
from .models import FIELD_NAMES
from .pipeline import ReportParser, write_debug
from .settings import load_settings, with_endpoint_overrides


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline, template-free RK3588 report image parser",
    )
    parser.add_argument("--config", type=Path, help="JSON config file")
    parser.add_argument("--image", type=Path, help="single JPEG or PNG report image")
    parser.add_argument("--output", type=Path, help="write JSON result to this file")
    parser.add_argument("--debug-dir", type=Path, help="explicitly write OCR diagnostics to this directory")
    parser.add_argument("--ocr-endpoint", help="override local PP-OCR endpoint")
    parser.add_argument("--llm-endpoint", help="override local RKLLM chat endpoint")
    parser.add_argument(
        "--linker-mode",
        choices=("chat", "constrained_choice", "evidence_choice", "evidence_chat"),
        default="chat",
        help="evidence_chat uses local chat for label/value IDs; evidence_choice uses constrained decoding",
    )
    parser.add_argument(
        "--association-mode",
        choices=("model_only", "hybrid", "evidence"),
        default="hybrid",
        help="hybrid emits only unambiguous OCR label/value pairs and discards unanchored model selections",
    )
    parser.add_argument(
        "--all-fields",
        action="store_true",
        help="legacy mode: run the original nine-field patient/report parser",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress non-sensitive progress messages on stderr")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "runtime" / "manifest.json",
        help="runtime manifest path",
    )
    parser.add_argument("--check-runtime", action="store_true", help="validate the RKLLM/runtime manifest and exit")
    parser.add_argument(
        "--allow-unverified-runtime",
        action="store_true",
        help="development-only: do not block parsing when the runtime manifest is incomplete",
    )
    return parser


def _emit(payload: object, output: Optional[Path]) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if output is None:
        sys.stdout.write(rendered)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")


def _runtime_check(manifest: Path) -> dict:
    try:
        return check_manifest(manifest)
    except ManifestError as exc:
        return {"ok": False, "errors": [str(exc)]}


def _progress_writer(quiet: bool):
    def emit(message: str) -> None:
        if not quiet:
            print("[report-parser] " + message, file=sys.stderr, flush=True)

    return emit


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    evidence_linker = args.linker_mode in {"evidence_choice", "evidence_chat"}
    if args.all_fields and evidence_linker != (args.association_mode == "evidence"):
        parser.error("evidence_choice/evidence_chat must be paired with evidence association mode")
    progress = _progress_writer(args.quiet)
    runtime = _runtime_check(args.manifest)
    if args.check_runtime:
        _emit(runtime, args.output)
        return 0 if runtime.get("ok") else 2
    if args.image is None:
        _build_parser().error("--image is required unless --check-runtime is used")
    if not args.image.is_file():
        _emit({"status": "error", "error": "image file not found"}, args.output)
        return 2
    if args.image.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
        _emit({"status": "error", "error": "only JPEG and PNG are supported"}, args.output)
        return 2
    if not runtime.get("ok") and not args.allow_unverified_runtime:
        _emit({"status": "error", "error": "runtime manifest is not verified", "details": runtime["errors"]}, args.output)
        return 2

    try:
        settings = load_settings(args.config)
        settings = with_endpoint_overrides(settings, args.ocr_endpoint, args.llm_endpoint)
        if args.all_fields:
            if args.linker_mode == "constrained_choice":
                linker = ConstrainedChoiceLinker(target_fields=FIELD_NAMES, progress_callback=progress)
            elif args.linker_mode == "evidence_choice":
                linker = EvidenceChoiceLinker(target_fields=FIELD_NAMES, progress_callback=progress)
            elif args.linker_mode == "evidence_chat":
                linker = EvidenceChatLinker(target_fields=FIELD_NAMES, progress_callback=progress)
            else:
                linker = None
            outcome = ReportParser(
                settings,
                linker=linker,
                association_mode=args.association_mode,
                target_fields=FIELD_NAMES,
                progress_callback=progress,
            ).parse_path(args.image)
        else:
            outcome = IdentifierParser(settings, progress_callback=progress).parse_path(args.image)
    except (OSError, ValueError, ServiceError) as exc:
        _emit({"status": "error", "error": str(exc)}, args.output)
        return 2

    if args.debug_dir is not None:
        if args.all_fields:
            write_debug(outcome, args.debug_dir)
        else:
            write_identifier_debug(outcome, args.debug_dir)
    payload = outcome.result.to_dict()
    _emit(payload, args.output)
    progress("complete: %s" % outcome.result.status)
    return 0 if outcome.result.status == "accepted" else 1
