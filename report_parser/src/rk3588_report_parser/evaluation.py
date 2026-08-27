from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import __version__
from .choice_linker import ConstrainedChoiceLinker
from .clients import FieldLinkerProtocol, ServiceError
from .evidence_chat_linker import EvidenceChatLinker
from .evidence_linker import EvidenceChoiceLinker
from .models import FIELD_NAMES
from .pipeline import ReportParser
from .settings import ParserSettings, load_settings, with_endpoint_overrides


class DatasetError(ValueError):
    pass


@dataclass(frozen=True)
class EvaluationSample:
    sample_id: str
    ocr_response: Dict[str, Any]
    image_size: Tuple[int, int]
    expected_fields: Dict[str, str]
    expected_status: str


def _json_object(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DatasetError("dataset fixture not found: %s" % path) from exc
    except json.JSONDecodeError as exc:
        raise DatasetError("invalid fixture JSON: %s" % path) from exc
    if not isinstance(value, dict):
        raise DatasetError("OCR fixture must be a JSON object")
    return value


def _load_raw_records(path: Path) -> Tuple[List[Dict[str, Any]], str]:
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError as exc:
        raise DatasetError("dataset not found: %s" % path) from exc
    digest = hashlib.sha256(raw_bytes).hexdigest()
    text = raw_bytes.decode("utf-8")
    if path.suffix.lower() == ".jsonl":
        records: List[Dict[str, Any]] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DatasetError("invalid JSONL at line %d" % line_number) from exc
            if not isinstance(record, dict):
                raise DatasetError("JSONL record %d must be an object" % line_number)
            records.append(record)
        return records, digest

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DatasetError("invalid dataset JSON") from exc
    if isinstance(parsed, dict):
        parsed = parsed.get("samples")
    if not isinstance(parsed, list):
        raise DatasetError("dataset JSON must be an array or contain a samples array")
    if not all(isinstance(record, dict) for record in parsed):
        raise DatasetError("dataset records must be objects")
    return list(parsed), digest


def _image_size(value: Any) -> Tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise DatasetError("image_size must be [width, height]")
    width, height = value
    if (
        not isinstance(width, int)
        or isinstance(width, bool)
        or not isinstance(height, int)
        or isinstance(height, bool)
        or width < 1
        or height < 1
    ):
        raise DatasetError("image_size values must be positive integers")
    return width, height


def _expected_fields(value: Any) -> Dict[str, str]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise DatasetError("expected must be an object")
    unknown = set(value) - set(FIELD_NAMES)
    if unknown:
        raise DatasetError("expected contains unsupported field(s): %s" % ", ".join(sorted(unknown)))
    fields: Dict[str, str] = {}
    for field in FIELD_NAMES:
        raw = value.get(field, "")
        if raw is None:
            fields[field] = ""
        elif isinstance(raw, (str, int, float)) and not isinstance(raw, bool):
            fields[field] = str(raw).strip()
        else:
            raise DatasetError("expected.%s must be a string, number, null, or omitted" % field)
    return fields


def _sample_from_record(record: Dict[str, Any], dataset_path: Path, index: int) -> EvaluationSample:
    forbidden = {"image", "image_path", "source_image", "source_path", "report_path"}
    present_forbidden = sorted(forbidden & set(record))
    if present_forbidden:
        raise DatasetError(
            "dataset record %d must not reference or embed an original image: %s"
            % (index, ", ".join(present_forbidden))
        )

    sample_id = record.get("id")
    if not isinstance(sample_id, str) or not sample_id.strip():
        raise DatasetError("dataset record %d needs a non-empty deidentified id" % index)

    has_inline = "ocr" in record
    has_file = "ocr_file" in record
    if has_inline == has_file:
        raise DatasetError("dataset record %d needs exactly one of ocr or ocr_file" % index)
    if has_inline:
        response = record["ocr"]
        if not isinstance(response, dict):
            raise DatasetError("dataset record %d ocr must be an object" % index)
    else:
        relative = record["ocr_file"]
        if not isinstance(relative, str) or not relative.strip():
            raise DatasetError("dataset record %d ocr_file must be a path string" % index)
        response = _json_object((dataset_path.parent / relative).resolve())

    image_size = _image_size(record.get("image_size", response.get("image_size")))
    expected_status = record.get("expected_status", "accepted")
    if expected_status not in {"accepted", "rejected"}:
        raise DatasetError("dataset record %d expected_status must be accepted or rejected" % index)
    return EvaluationSample(
        sample_id=sample_id.strip(),
        ocr_response=response,
        image_size=image_size,
        expected_fields=_expected_fields(record.get("expected")),
        expected_status=expected_status,
    )


def load_dataset(path: Path) -> Tuple[List[EvaluationSample], str]:
    records, digest = _load_raw_records(path)
    if not records:
        raise DatasetError("dataset has no samples")
    samples = [_sample_from_record(record, path, index) for index, record in enumerate(records, start=1)]
    ids = [sample.sample_id for sample in samples]
    if len(ids) != len(set(ids)):
        raise DatasetError("dataset sample ids must be unique")
    return samples, digest


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def _empty_field_counts() -> Dict[str, Dict[str, int]]:
    return {
        field: {
            "expected_present": 0,
            "actual_present": 0,
            "correct": 0,
            "missing": 0,
            "incorrect": 0,
            "unexpected": 0,
        }
        for field in FIELD_NAMES
    }


def evaluate_samples(
    samples: Sequence[EvaluationSample],
    settings: ParserSettings,
    linker: Optional[FieldLinkerProtocol] = None,
    association_mode: str = "model_only",
) -> Dict[str, Any]:
    """Evaluate semantic field linking with OCR fixtures and no report images."""

    parser = ReportParser(settings, linker=linker, association_mode=association_mode)
    field_counts = _empty_field_counts()
    acceptance = {
        "true_accepted": 0,
        "false_accepted": 0,
        "true_rejected": 0,
        "false_rejected": 0,
    }
    reason_counts: Dict[str, int] = {}
    rows: List[Dict[str, Any]] = []
    elapsed_values: List[float] = []
    strict_matches = 0
    operational_errors = 0

    for sample in samples:
        started = time.monotonic()
        try:
            outcome = parser.parse_ocr_response(sample.ocr_response, sample.image_size)
        except (OSError, ValueError, ServiceError) as exc:
            elapsed = round((time.monotonic() - started) * 1000, 2)
            operational_errors += 1
            rows.append(
                {
                    "id": sample.sample_id,
                    "expected_status": sample.expected_status,
                    "actual_status": "error",
                    "status_match": False,
                    "field_mismatches": ["operational_error"],
                    "elapsed_ms": elapsed,
                    "error": str(exc),
                }
            )
            continue

        elapsed = round((time.monotonic() - started) * 1000, 2)
        elapsed_values.append(elapsed)
        actual_status = outcome.result.status
        if sample.expected_status == "accepted":
            if actual_status == "accepted":
                acceptance["true_accepted"] += 1
            else:
                acceptance["false_rejected"] += 1
        elif actual_status == "accepted":
            acceptance["false_accepted"] += 1
        else:
            acceptance["true_rejected"] += 1

        mismatches: List[str] = []
        for field in FIELD_NAMES:
            expected = sample.expected_fields[field]
            actual = outcome.result.fields[field].value
            expected_present = bool(expected)
            actual_present = bool(actual)
            counts = field_counts[field]
            if expected_present:
                counts["expected_present"] += 1
            if actual_present:
                counts["actual_present"] += 1
            if expected_present and actual == expected:
                counts["correct"] += 1
            elif expected_present and not actual_present:
                counts["missing"] += 1
                mismatches.append(field)
            elif expected_present:
                counts["incorrect"] += 1
                mismatches.append(field)
            elif actual_present:
                counts["unexpected"] += 1
                mismatches.append(field)

        status_match = actual_status == sample.expected_status
        if not status_match:
            mismatches.insert(0, "status")
        if status_match and not mismatches:
            strict_matches += 1
        for reason in outcome.result.rejection_reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        rows.append(
            {
                "id": sample.sample_id,
                "expected_status": sample.expected_status,
                "actual_status": actual_status,
                "status_match": status_match,
                "field_mismatches": mismatches,
                "elapsed_ms": elapsed,
                "rejection_reasons": outcome.result.rejection_reasons,
                "association": outcome.result.association,
            }
        )

    field_metrics: Dict[str, Dict[str, Any]] = {}
    for field, counts in field_counts.items():
        field_metrics[field] = {
            **counts,
            "expected_value_exact_rate": _ratio(counts["correct"], counts["expected_present"]),
            "precision": _ratio(counts["correct"], counts["actual_present"]),
            "recall": _ratio(counts["correct"], counts["expected_present"]),
        }
    return {
        "version": __version__,
        "mode": "ocr_span_semantic_benchmark",
        "association_mode": association_mode,
        "samples_total": len(samples),
        "samples_processed": len(samples) - operational_errors,
        "operational_error_count": operational_errors,
        "acceptance": acceptance,
        "strict_sample_match_rate": _ratio(strict_matches, len(samples)),
        "field_metrics": field_metrics,
        "latency_ms": {
            "mean": round(sum(elapsed_values) / len(elapsed_values), 2) if elapsed_values else None,
            "max": round(max(elapsed_values), 2) if elapsed_values else None,
        },
        "rejection_reason_counts": dict(sorted(reason_counts.items())),
        "samples": rows,
    }


DEPLOYMENT_TARGETS = {
    "patient_id": 0.95,
    "report_no": 0.95,
    "patient_name": 0.90,
    "sex": 0.90,
    "birthday": 0.90,
    "exam_item": 0.90,
}


def deployment_target_failures(report: Dict[str, Any]) -> List[str]:
    failures: List[str] = []
    if report["samples_total"] < 50:
        failures.append("dataset has fewer than 50 samples")
    if report["operational_error_count"]:
        failures.append("benchmark has operational errors")
    if report["acceptance"]["false_accepted"]:
        failures.append("untrusted samples were accepted")
    for field, target in DEPLOYMENT_TARGETS.items():
        metric = report["field_metrics"][field]
        rate = metric["expected_value_exact_rate"]
        if metric["expected_present"] == 0:
            failures.append("%s has no labeled value" % field)
        elif rate is None or rate < target:
            failures.append("%s exact rate %s is below %.2f" % (field, rate, target))
    return failures


def _emit(payload: Dict[str, Any], output: Optional[Path]) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if output is None:
        sys.stdout.write(rendered)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate local model field linking with deidentified OCR fixtures",
    )
    parser.add_argument("--dataset", type=Path, required=True, help="JSON or JSONL deidentified OCR dataset")
    parser.add_argument("--config", type=Path, help="desktop local-service JSON config")
    parser.add_argument("--output", type=Path, help="write aggregate JSON report here")
    parser.add_argument("--llm-endpoint", help="override local OpenAI-compatible chat endpoint")
    parser.add_argument(
        "--association-mode",
        choices=("model_only", "hybrid", "evidence"),
        default="model_only",
        help="evidence validates model-selected label and value span IDs",
    )
    parser.add_argument(
        "--linker-mode",
        choices=("chat", "constrained_choice", "evidence_choice", "evidence_chat"),
        default="chat",
        help="evidence_chat uses normal local chat to select validated evidence options",
    )
    parser.add_argument(
        "--fail-on-mismatch",
        action="store_true",
        help="return exit code 1 unless every sample status and field matches",
    )
    parser.add_argument(
        "--enforce-deployment-targets",
        action="store_true",
        help="require 50 samples, target field rates, and no false accepts",
    )
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    evidence_linker = args.linker_mode in {"evidence_choice", "evidence_chat"}
    if evidence_linker != (args.association_mode == "evidence"):
        _build_parser().error("evidence_choice/evidence_chat must be paired with evidence association mode")
    try:
        settings = with_endpoint_overrides(load_settings(args.config), llm_endpoint=args.llm_endpoint)
        samples, dataset_digest = load_dataset(args.dataset)
        if args.linker_mode == "constrained_choice":
            linker = ConstrainedChoiceLinker()
        elif args.linker_mode == "evidence_choice":
            linker = EvidenceChoiceLinker()
        elif args.linker_mode == "evidence_chat":
            linker = EvidenceChatLinker()
        else:
            linker = None
        report = evaluate_samples(
            samples,
            settings,
            linker=linker,
            association_mode=args.association_mode,
        )
    except (DatasetError, OSError, ValueError) as exc:
        _emit({"status": "error", "error": str(exc)}, args.output)
        return 2

    report["dataset_sha256"] = dataset_digest
    report["linker_mode"] = args.linker_mode
    target_failures: List[str] = []
    if args.enforce_deployment_targets:
        target_failures = deployment_target_failures(report)
        report["deployment_gate"] = {"passed": not target_failures, "failures": target_failures}
    _emit(report, args.output)

    if report["operational_error_count"]:
        return 2
    if args.enforce_deployment_targets and target_failures:
        return 1
    if args.fail_on_mismatch and report["strict_sample_match_rate"] != 1.0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
