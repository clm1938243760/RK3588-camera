from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from rk3588_report_parser.evaluation import (
    EvaluationSample,
    deployment_target_failures,
    evaluate_samples,
    load_dataset,
    main,
)
from rk3588_report_parser.models import FIELD_NAMES
from rk3588_report_parser.settings import LlmSettings, OcrSettings, ParserSettings, QualitySettings, ValidationSettings


def parser_settings() -> ParserSettings:
    return ParserSettings(
        ocr=OcrSettings(endpoint="http://127.0.0.1:5002/ocr", timeout_seconds=5),
        llm=LlmSettings(
            endpoint="http://127.0.0.1:8010/v1/chat/completions",
            model="desktop-test",
            timeout_seconds=5,
            max_tokens=256,
        ),
        quality=QualitySettings(
            min_longest_side=1600,
            min_contrast=6,
            min_laplacian_energy=25,
            min_ocr_items=3,
            min_ocr_score=0.65,
        ),
        validation=ValidationSettings(max_age_delta_years=2, require_patient_name=True, require_identifier=True),
    )


def fixture() -> dict:
    rows = [
        (80, "Name", "Alice"),
        (150, "Patient ID", "P2605260007"),
        (220, "Sex", "male"),
        (290, "Age", "45"),
        (360, "Birthday", "1981-02-03"),
        (430, "Report No", "R202608100001"),
        (500, "Report Date", "2026-08-10"),
        (570, "Exam Item", "Abdominal ultrasound"),
    ]
    ocr = []
    for y, label, value in rows:
        ocr.append({"text": label, "score": 0.98, "box": [80, y, 180, y + 35]})
        ocr.append({"text": value, "score": 0.98, "box": [240, y, 620, y + 35]})
    return {"ok": True, "image_size": [1800, 2000], "ocr": ocr}


class FakeLinker:
    def link(self, system_prompt, user_prompt, settings):
        payload = {field: {"span_ids": []} for field in FIELD_NAMES}
        payload.update(
            {
                "patient_name": {"span_ids": [2]},
                "patient_id": {"span_ids": [4]},
                "sex": {"span_ids": [6]},
                "age": {"span_ids": [8]},
                "birthday": {"span_ids": [10]},
                "report_no": {"span_ids": [12]},
                "report_date": {"span_ids": [14]},
                "exam_item": {"span_ids": [16]},
            }
        )
        return json.dumps(payload)


class _ChatHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        response = {"choices": [{"message": {"content": FakeLinker().link("", "", None)}}]}
        body = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _SpanChoiceHandler(BaseHTTPRequestHandler):
    choices = {
        "patient_name": 2,
        "patient_id": 4,
        "sex": 6,
        "age": 8,
        "birthday": 10,
        "his_exam_no": 0,
        "report_no": 12,
        "report_date": 14,
        "exam_item": 16,
    }

    def log_message(self, format, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if self.path != "/v1/span-choice":
            self.send_response(404)
            self.end_headers()
            return
        prompt = json.loads(payload["messages"][-1]["content"])
        choice_id = self.choices[prompt["field"]]
        response = {"choice_id": choice_id}
        body = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def sample(expected_status: str = "accepted") -> EvaluationSample:
    expected = {
        "patient_name": "Alice",
        "patient_id": "P2605260007",
        "sex": "\u7537",
        "age": "45",
        "birthday": "1981-02-03",
        "his_exam_no": "",
        "report_no": "R202608100001",
        "report_date": "2026-08-10",
        "exam_item": "Abdominal ultrasound",
    }
    return EvaluationSample(
        sample_id="anon-001",
        ocr_response=fixture(),
        image_size=(1800, 2000),
        expected_fields=expected,
        expected_status=expected_status,
    )


class EvaluationTests(unittest.TestCase):
    def test_evaluates_same_model_contract_without_an_image(self) -> None:
        report = evaluate_samples([sample()], parser_settings(), linker=FakeLinker())

        self.assertEqual(report["operational_error_count"], 0)
        self.assertEqual(report["strict_sample_match_rate"], 1.0)
        self.assertEqual(report["acceptance"]["true_accepted"], 1)
        self.assertEqual(report["field_metrics"]["patient_id"]["expected_value_exact_rate"], 1.0)
        self.assertEqual(report["samples"][0]["field_mismatches"], [])

    def test_reports_false_accept_for_rejected_gold_sample(self) -> None:
        report = evaluate_samples([sample(expected_status="rejected")], parser_settings(), linker=FakeLinker())

        self.assertEqual(report["acceptance"]["false_accepted"], 1)
        self.assertIn("untrusted samples were accepted", deployment_target_failures(report))

    def test_loads_deidentified_ocr_json_and_rejects_image_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "dataset.json"
            dataset.write_text(
                json.dumps(
                    [
                        {
                            "id": "anon-001",
                            "ocr": fixture(),
                            "expected": sample().expected_fields,
                            "expected_status": "accepted",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            samples, digest = load_dataset(dataset)
            self.assertEqual(samples[0].sample_id, "anon-001")
            self.assertEqual(len(digest), 64)

            dataset.write_text(
                json.dumps(
                    [
                        {
                            "id": "invalid",
                            "image_path": "report.jpg",
                            "ocr": fixture(),
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_dataset(dataset)

    def test_checked_in_smoke_fixture_is_deidentified_and_valid(self) -> None:
        root = Path(__file__).resolve().parents[1]
        samples, _ = load_dataset(root / "fixtures" / "pc_smoke_dataset.json")

        self.assertEqual(samples[0].sample_id, "synthetic-smoke-001")
        self.assertEqual(samples[0].expected_fields["patient_id"], "P2605260007")

    def test_cli_evaluator_calls_loopback_model_service(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _ChatHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                dataset = root / "dataset.json"
                output = root / "result.json"
                dataset.write_text(
                    json.dumps(
                        [
                            {
                                "id": "anon-001",
                                "ocr": fixture(),
                                "expected": sample().expected_fields,
                            }
                        ]
                    ),
                    encoding="utf-8",
                )
                code = main(
                    [
                        "--dataset",
                        str(dataset),
                        "--llm-endpoint",
                        "http://127.0.0.1:%d/v1/chat/completions" % server.server_port,
                        "--output",
                        str(output),
                        "--fail-on-mismatch",
                    ]
                )
                report = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(code, 0)
                self.assertEqual(report["strict_sample_match_rate"], 1.0)
        finally:
            server.shutdown()
            server.server_close()

    def test_cli_evaluator_runs_constrained_choice_linker(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _SpanChoiceHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                dataset = root / "dataset.json"
                output = root / "result.json"
                dataset.write_text(
                    json.dumps(
                        [
                            {
                                "id": "anon-001",
                                "ocr": fixture(),
                                "expected": sample().expected_fields,
                            }
                        ]
                    ),
                    encoding="utf-8",
                )
                code = main(
                    [
                        "--dataset",
                        str(dataset),
                        "--llm-endpoint",
                        "http://127.0.0.1:%d/v1/chat/completions" % server.server_port,
                        "--linker-mode",
                        "constrained_choice",
                        "--output",
                        str(output),
                        "--fail-on-mismatch",
                    ]
                )
                report = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(code, 0)
                self.assertEqual(report["linker_mode"], "constrained_choice")
                self.assertEqual(report["strict_sample_match_rate"], 1.0)
        finally:
            server.shutdown()
            server.server_close()

    def test_deployment_gate_rejects_a_perfect_but_too_small_dataset(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _ChatHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                dataset = root / "dataset.json"
                output = root / "result.json"
                dataset.write_text(
                    json.dumps(
                        [
                            {
                                "id": "anon-001",
                                "ocr": fixture(),
                                "expected": sample().expected_fields,
                            }
                        ]
                    ),
                    encoding="utf-8",
                )
                code = main(
                    [
                        "--dataset",
                        str(dataset),
                        "--llm-endpoint",
                        "http://127.0.0.1:%d/v1/chat/completions" % server.server_port,
                        "--output",
                        str(output),
                        "--enforce-deployment-targets",
                    ]
                )
                report = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(code, 1)
                self.assertFalse(report["deployment_gate"]["passed"])
                self.assertIn("dataset has fewer than 50 samples", report["deployment_gate"]["failures"])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
