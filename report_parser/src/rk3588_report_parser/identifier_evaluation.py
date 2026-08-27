from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import __version__
from .clients import ServiceError
from .identifier_models import IDENTIFIER_TYPES
from .identifier_pipeline import IdentifierParser
from .settings import load_settings, with_endpoint_overrides


class IdentifierDatasetError(ValueError):
    pass


@dataclass(frozen=True)
class IdentifierEvaluationSample:
    sample_id: str
    ocr_response: Dict[str, Any]
    image_size: Tuple[int, int]
    expected_status: str
    expected_identifiers: Tuple[Tuple[str, str], ...]
    expected_primary: Optional[Tuple[str, str]]


def _read_records(path: Path) -> Tuple[List[Dict[str, Any]], str]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise IdentifierDatasetError("dataset not found: %s" % path) from exc
    digest = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8")
        if path.suffix.lower() == ".jsonl":
            records = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            payload = json.loads(text)
            records = payload.get("samples") if isinstance(payload, dict) else payload
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IdentifierDatasetError("dataset is not valid UTF-8 JSON/JSONL") from exc
    if not isinstance(records, list) or not records or not all(isinstance(item, dict) for item in records):
        raise IdentifierDatasetError("dataset must contain a non-empty sample array")
    return records, digest


def _load_ocr(record: Dict[str, Any], dataset_path: Path, index: int) -> Dict[str, Any]:
    forbidden = {"image", "image_path", "source_image", "source_path", "report_path"}
    if forbidden & set(record):
        raise IdentifierDatasetError("sample %d must not contain an image or image path" % index)
    inline = record.get("ocr")
    relative = record.get("ocr_file")
    if (inline is None) == (relative is None):
        raise IdentifierDatasetError("sample %d needs exactly one of ocr or ocr_file" % index)
    if inline is not None:
        if not isinstance(inline, dict):
            raise IdentifierDatasetError("sample %d ocr must be an object" % index)
        return inline
    if not isinstance(relative, str) or not relative.strip():
        raise IdentifierDatasetError("sample %d ocr_file must be a path string" % index)
    try:
        payload = json.loads((dataset_path.parent / relative).resolve().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IdentifierDatasetError("sample %d OCR fixture cannot be read" % index) from exc
    if not isinstance(payload, dict):
        raise IdentifierDatasetError("sample %d OCR fixture must be an object" % index)
    return payload


def _pair(value: Any, name: str, index: int) -> Tuple[str, str]:
    if not isinstance(value, dict) or set(value) != {"type", "value"}:
        raise IdentifierDatasetError("sample %d %s must contain type and value" % (index, name))
    identifier_type = value["type"]
    identifier_value = value["value"]
    if identifier_type not in IDENTIFIER_TYPES:
        raise IdentifierDatasetError("sample %d %s has an unsupported type" % (index, name))
    if not isinstance(identifier_value, str) or not identifier_value.strip():
        raise IdentifierDatasetError("sample %d %s needs a non-empty string value" % (index, name))
    return str(identifier_type), identifier_value.strip()


def load_identifier_dataset(path: Path) -> Tuple[List[IdentifierEvaluationSample], str]:
    records, digest = _read_records(path)
    samples: List[IdentifierEvaluationSample] = []
    seen_ids = set()
    for index, record in enumerate(records, start=1):
        sample_id = record.get("id")
        if not isinstance(sample_id, str) or not sample_id.strip() or sample_id in seen_ids:
            raise IdentifierDatasetError("sample %d needs a unique non-empty id" % index)
        seen_ids.add(sample_id)
        size = record.get("image_size")
        if (
            not isinstance(size, list)
            or len(size) != 2
            or not all(isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in size)
        ):
            raise IdentifierDatasetError("sample %d image_size must be [width, height]" % index)
        status = record.get("expected_status")
        if status not in {"accepted", "review_required", "rejected"}:
            raise IdentifierDatasetError("sample %d has an invalid expected_status" % index)
        raw_identifiers = record.get("expected_identifiers", [])
        if not isinstance(raw_identifiers, list):
            raise IdentifierDatasetError("sample %d expected_identifiers must be an array" % index)
        identifiers = tuple(_pair(value, "expected_identifiers", index) for value in raw_identifiers)
        if len(identifiers) != len(set(identifiers)):
            raise IdentifierDatasetError("sample %d repeats an expected identifier" % index)
        primary_raw = record.get("expected_primary")
        primary = None if primary_raw is None else _pair(primary_raw, "expected_primary", index)
        if primary is not None and primary not in identifiers:
            raise IdentifierDatasetError("sample %d primary identifier is not in expected_identifiers" % index)
        samples.append(
            IdentifierEvaluationSample(
                sample_id.strip(),
                _load_ocr(record, path, index),
                (size[0], size[1]),
                status,
                identifiers,
                primary,
            )
        )
    return samples, digest


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 4) if denominator else None


def _percentile(values: Sequence[float], percentile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 2)


def evaluate_identifier_samples(
    samples: Sequence[IdentifierEvaluationSample],
    parser: IdentifierParser,
) -> Dict[str, Any]:
    predicted_total = correct_total = expected_total = 0
    accepted_predicted = accepted_correct = 0
    primary_expected = primary_correct = 0
    strict_matches = false_accepted = operational_errors = review_count = 0
    status_counts: Dict[str, int] = {}
    type_confusions: Dict[str, int] = {}
    rows: List[Dict[str, Any]] = []
    latencies: List[float] = []

    for sample in samples:
        started = time.monotonic()
        try:
            outcome = parser.parse_ocr_response(sample.ocr_response, sample.image_size)
            result = outcome.result
        except (OSError, ValueError, ServiceError) as exc:
            operational_errors += 1
            rows.append({"id": sample.sample_id, "actual_status": "error", "error": str(exc)})
            continue
        elapsed = (time.monotonic() - started) * 1000.0
        latencies.append(elapsed)
        actual = {(item.type, item.value) for item in result.identifiers}
        expected = set(sample.expected_identifiers)
        correct = actual & expected
        predicted_total += len(actual)
        correct_total += len(correct)
        expected_total += len(expected)
        if result.status == "accepted":
            accepted_predicted += len(actual)
            accepted_correct += len(correct)
            if sample.expected_status != "accepted":
                false_accepted += 1
        if result.status == "review_required":
            review_count += 1
        status_counts[result.status] = status_counts.get(result.status, 0) + 1

        actual_primary = None
        if result.primary_identifier is not None:
            actual_primary = (result.primary_identifier.type, result.primary_identifier.value)
        if sample.expected_primary is not None:
            primary_expected += 1
            if actual_primary == sample.expected_primary:
                primary_correct += 1

        expected_by_value = {value: identifier_type for identifier_type, value in expected}
        for identifier_type, value in actual - correct:
            if value in expected_by_value:
                key = "%s->%s" % (expected_by_value[value], identifier_type)
                type_confusions[key] = type_confusions.get(key, 0) + 1

        status_match = result.status == sample.expected_status
        strict = status_match and actual == expected and actual_primary == sample.expected_primary
        strict_matches += int(strict)
        rows.append(
            {
                "id": sample.sample_id,
                "expected_status": sample.expected_status,
                "actual_status": result.status,
                "status_match": status_match,
                "missing": [dict(type=t, value=v) for t, v in sorted(expected - actual)],
                "unexpected": [dict(type=t, value=v) for t, v in sorted(actual - expected)],
                "primary_match": actual_primary == sample.expected_primary,
                "elapsed_ms": round(elapsed, 2),
            }
        )

    return {
        "version": __version__,
        "mode": "medical_application_identifier_benchmark",
        "samples_total": len(samples),
        "samples_processed": len(samples) - operational_errors,
        "operational_error_count": operational_errors,
        "false_accepted_samples": false_accepted,
        "review_ratio": _ratio(review_count, len(samples)),
        "strict_sample_match_rate": _ratio(strict_matches, len(samples)),
        "metrics": {
            "identifier_precision": _ratio(correct_total, predicted_total),
            "accepted_identifier_precision": _ratio(accepted_correct, accepted_predicted),
            "core_identifier_recall": _ratio(correct_total, expected_total),
            "primary_identifier_accuracy": _ratio(primary_correct, primary_expected),
        },
        "counts": {
            "predicted_identifiers": predicted_total,
            "correct_identifiers": correct_total,
            "expected_identifiers": expected_total,
            "expected_primary": primary_expected,
            "correct_primary": primary_correct,
        },
        "status_counts": dict(sorted(status_counts.items())),
        "type_confusions": dict(sorted(type_confusions.items())),
        "latency_ms": {
            "mean": round(sum(latencies) / len(latencies), 2) if latencies else None,
            "p95": _percentile(latencies, 0.95),
            "max": round(max(latencies), 2) if latencies else None,
        },
        "samples": rows,
    }


def deployment_gate_failures(report: Dict[str, Any]) -> List[str]:
    failures = []
    if report["samples_total"] < 100:
        failures.append("dataset has fewer than 100 blind-test samples")
    if report["operational_error_count"]:
        failures.append("benchmark has operational errors")
    if report["false_accepted_samples"]:
        failures.append("blind set contains false accepted samples")
    thresholds = {
        "accepted_identifier_precision": 0.995,
        "primary_identifier_accuracy": 0.995,
        "core_identifier_recall": 0.95,
    }
    for name, threshold in thresholds.items():
        value = report["metrics"].get(name)
        if value is None or value < threshold:
            failures.append("%s is below %.3f" % (name, threshold))
    return failures


def _emit(payload: Dict[str, Any], output: Optional[Path]) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if output is None:
        sys.stdout.write(rendered)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate the medical identifier parser on OCR fixtures")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--llm-endpoint")
    parser.add_argument("--enforce-deployment-targets", action="store_true")
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        settings = with_endpoint_overrides(load_settings(args.config), llm_endpoint=args.llm_endpoint)
        samples, digest = load_identifier_dataset(args.dataset)
        report = evaluate_identifier_samples(samples, IdentifierParser(settings))
    except (IdentifierDatasetError, OSError, ValueError, ServiceError) as exc:
        _emit({"status": "error", "error": str(exc)}, args.output)
        return 2
    report["dataset_sha256"] = digest
    failures = deployment_gate_failures(report) if args.enforce_deployment_targets else []
    if args.enforce_deployment_targets:
        report["deployment_gate"] = {"passed": not failures, "failures": failures}
    _emit(report, args.output)
    if report["operational_error_count"]:
        return 2
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
