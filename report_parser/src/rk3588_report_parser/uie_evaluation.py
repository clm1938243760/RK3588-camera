from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from .uie_extraction import (
    PaddleTaskflowEngine,
    blocks_from_payload,
    build_evidence_document,
    load_uie_schema,
    run_uie_extraction,
    uie_prompts,
)


class UieDatasetError(ValueError):
    pass


@dataclass(frozen=True)
class UieEvaluationSample:
    sample_id: str
    blocks: tuple[dict[str, Any], ...]
    expected: dict[str, str]
    expected_status: str


def load_uie_dataset(
    path: Path,
    schema: Sequence[Mapping[str, Any]],
) -> tuple[list[UieEvaluationSample], str]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise UieDatasetError("dataset not found: %s" % path) from exc
    digest = hashlib.sha256(raw).hexdigest()
    try:
        if path.suffix.lower() == ".jsonl":
            records = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
        else:
            payload = json.loads(raw.decode("utf-8"))
            records = payload.get("samples") if isinstance(payload, dict) else payload
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UieDatasetError("dataset is not valid UTF-8 JSON or JSONL") from exc
    if not isinstance(records, list) or not records or not all(isinstance(item, dict) for item in records):
        raise UieDatasetError("dataset must contain a non-empty sample array")

    schema_keys = {str(item["field_key"]) for item in schema}
    seen_ids = set()
    samples = []
    for index, record in enumerate(records, start=1):
        forbidden = {"image", "image_path", "source_image", "source_path", "report_path"}
        if forbidden & set(record):
            raise UieDatasetError("sample %d must not contain an image or image path" % index)
        sample_id = record.get("id")
        if not isinstance(sample_id, str) or not sample_id.strip() or sample_id in seen_ids:
            raise UieDatasetError("sample %d needs a unique non-empty id" % index)
        seen_ids.add(sample_id)
        inline = record.get("ocr")
        relative = record.get("ocr_file")
        if (inline is None) == (relative is None):
            raise UieDatasetError("sample %d needs exactly one of ocr or ocr_file" % index)
        if relative is not None:
            if not isinstance(relative, str) or not relative.strip():
                raise UieDatasetError("sample %d ocr_file must be a path string" % index)
            try:
                ocr_payload = json.loads((path.parent / relative).resolve().read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise UieDatasetError("sample %d OCR fixture cannot be read" % index) from exc
        else:
            ocr_payload = inline
        try:
            blocks = blocks_from_payload(ocr_payload)
        except ValueError as exc:
            raise UieDatasetError("sample %d has no valid OCR blocks" % index) from exc
        expected_raw = record.get("expected", {})
        if not isinstance(expected_raw, dict):
            raise UieDatasetError("sample %d expected must be an object" % index)
        unknown = set(expected_raw) - schema_keys
        if unknown:
            raise UieDatasetError(
                "sample %d expected has field(s) absent from UIE schema: %s"
                % (index, ", ".join(sorted(unknown)))
            )
        expected = {}
        for key in schema_keys:
            value = expected_raw.get(key, "")
            if value is None:
                expected[key] = ""
            elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
                expected[key] = str(value).strip()
            else:
                raise UieDatasetError("sample %d expected.%s must be scalar" % (index, key))
        expected_status = record.get("expected_status", "accepted")
        if expected_status not in {"accepted", "review_required", "rejected"}:
            raise UieDatasetError("sample %d has an invalid expected_status" % index)
        samples.append(UieEvaluationSample(sample_id.strip(), tuple(blocks), expected, expected_status))
    return samples, digest


def evaluate_uie_samples(
    samples: Sequence[UieEvaluationSample],
    schema: Sequence[Mapping[str, Any]],
    model: str,
    predictor: Callable[[str], Mapping[str, Any]],
) -> dict[str, Any]:
    keys = [str(item["field_key"]) for item in schema]
    counts = {
        key: {
            "expected_present": 0,
            "predicted_present": 0,
            "exact": 0,
            "missing": 0,
            "incorrect": 0,
            "unexpected": 0,
            "evidence_traced": 0,
        }
        for key in keys
    }
    rows = []
    latencies = []
    status_counts: dict[str, int] = {}
    strict_matches = 0
    operational_errors = 0
    predicted_total = 0
    evidence_traced_total = 0

    for sample in samples:
        try:
            result = run_uie_extraction(sample.blocks, schema, model, predictor)
        except Exception as exc:
            operational_errors += 1
            rows.append({
                "id": sample.sample_id,
                "actual_status": "error",
                "error": type(exc).__name__,
            })
            continue
        actual_status = str(result.get("status", "error"))
        status_counts[actual_status] = status_counts.get(actual_status, 0) + 1
        elapsed = float(result.get("timings", {}).get("uie_ms", 0.0))
        latencies.append(elapsed)
        document = build_evidence_document(sample.blocks)
        segment_ids = {segment.span_id for segment in document.segments}
        mismatches = []
        actual_fields = result.get("fields", {})
        for key in keys:
            expected = sample.expected.get(key, "")
            evidence = actual_fields.get(key) if isinstance(actual_fields, dict) else None
            actual = str(evidence.get("value", "")) if isinstance(evidence, dict) else ""
            field_counts = counts[key]
            if expected:
                field_counts["expected_present"] += 1
            if actual:
                field_counts["predicted_present"] += 1
                predicted_total += 1
                source_ids = evidence.get("source_span_ids", []) if isinstance(evidence, dict) else []
                traced = (
                    isinstance(source_ids, list)
                    and bool(source_ids)
                    and all(isinstance(value, int) and value in segment_ids for value in source_ids)
                    and actual in document.text
                )
                if traced:
                    field_counts["evidence_traced"] += 1
                    evidence_traced_total += 1
            if expected and actual == expected:
                field_counts["exact"] += 1
            elif expected and not actual:
                field_counts["missing"] += 1
                mismatches.append(key)
            elif expected:
                field_counts["incorrect"] += 1
                mismatches.append(key)
            elif actual:
                field_counts["unexpected"] += 1
                mismatches.append(key)
        status_match = actual_status == sample.expected_status
        strict = status_match and not mismatches
        strict_matches += int(strict)
        rows.append({
            "id": sample.sample_id,
            "expected_status": sample.expected_status,
            "actual_status": actual_status,
            "status_match": status_match,
            "field_mismatches": mismatches,
            "elapsed_ms": round(elapsed, 2),
        })

    field_metrics = {}
    for key, field_counts in counts.items():
        field_metrics[key] = {
            **field_counts,
            "precision": _ratio(field_counts["exact"], field_counts["predicted_present"]),
            "recall": _ratio(field_counts["exact"], field_counts["expected_present"]),
            "evidence_trace_rate": _ratio(
                field_counts["evidence_traced"], field_counts["predicted_present"]
            ),
        }
    return {
        "schema_version": 1,
        "mode": "uie_ocr_evidence_benchmark",
        "model": model,
        "samples_total": len(samples),
        "samples_processed": len(samples) - operational_errors,
        "operational_error_count": operational_errors,
        "strict_sample_match_rate": _ratio(strict_matches, len(samples)),
        "evidence_trace_rate": _ratio(evidence_traced_total, predicted_total),
        "status_counts": dict(sorted(status_counts.items())),
        "field_metrics": field_metrics,
        "latency_ms": {
            "mean": round(sum(latencies) / len(latencies), 2) if latencies else None,
            "p95": _percentile(latencies, 0.95),
            "max": round(max(latencies), 2) if latencies else None,
        },
        "samples": rows,
    }


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 4) if denominator else None


def _percentile(values: Sequence[float], percentile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 2)


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate PaddleNLP UIE against deidentified OCR evidence fixtures"
    )
    parser.add_argument("--dataset", type=Path, required=True)
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        schema = load_uie_schema(args.schema)
        samples, digest = load_uie_dataset(args.dataset, schema)
        engine = PaddleTaskflowEngine(
            args.model,
            uie_prompts(schema),
            device=args.device,
            position_prob=args.position_prob,
            max_seq_len=args.max_seq_len,
        )
        report = evaluate_uie_samples(samples, schema, args.model, engine.predict)
        report["dataset_sha256"] = digest
    except Exception as exc:
        report = {
            "schema_version": 1,
            "status": "error",
            "model": args.model,
            "error": type(exc).__name__,
        }
    if args.output is None:
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    else:
        _atomic_write(args.output, report)
    if report.get("status") == "error" or report.get("operational_error_count"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
