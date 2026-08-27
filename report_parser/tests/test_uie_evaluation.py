from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from rk3588_report_parser.uie_evaluation import (
    evaluate_uie_samples,
    load_uie_dataset,
)


SCHEMA = [
    {"field_key": "patient_name", "prompt": "patient name", "minimum_probability": 0.5},
    {"field_key": "patient_id", "prompt": "patient id", "minimum_probability": 0.5},
]


class UieEvaluationTests(unittest.TestCase):
    def test_metrics_and_rows_do_not_repeat_patient_values(self) -> None:
        dataset = [{
            "id": "sample-001",
            "ocr": [
                {"id": 1, "line_id": 1, "text": "Name", "score": 0.99, "box": [0, 0, 20, 10]},
                {"id": 2, "line_id": 1, "text": "TestPerson", "score": 0.98, "box": [30, 0, 90, 10]},
                {"id": 3, "line_id": 2, "text": "ID", "score": 0.99, "box": [0, 20, 20, 30]},
                {"id": 4, "line_id": 2, "text": "P12345", "score": 0.98, "box": [30, 20, 90, 30]},
            ],
            "expected": {"patient_name": "TestPerson", "patient_id": "P12345"},
            "expected_status": "accepted",
        }]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dataset.json"
            path.write_text(json.dumps(dataset), encoding="utf-8")
            samples, _ = load_uie_dataset(path, SCHEMA)

        def predict(text):
            name_start = text.index("TestPerson")
            id_start = text.index("P12345")
            return {
                "patient name": [{
                    "text": "TestPerson", "start": name_start, "end": name_start + 10, "probability": 0.99,
                }],
                "patient id": [{
                    "text": "P12345", "start": id_start, "end": id_start + 6, "probability": 0.98,
                }],
            }

        report = evaluate_uie_samples(samples, SCHEMA, "fake-uie", predict)
        self.assertEqual(report["strict_sample_match_rate"], 1.0)
        self.assertEqual(report["evidence_trace_rate"], 1.0)
        self.assertEqual(report["field_metrics"]["patient_id"]["precision"], 1.0)
        self.assertNotIn("TestPerson", json.dumps(report["samples"]))
        self.assertNotIn("P12345", json.dumps(report["samples"]))

    def test_expected_field_must_exist_in_schema(self) -> None:
        dataset = [{
            "id": "sample-001",
            "ocr": [{"text": "A", "score": 0.9, "box": [0, 0, 1, 1]}],
            "expected": {"unknown": "A"},
        }]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dataset.json"
            path.write_text(json.dumps(dataset), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "absent from UIE schema"):
                load_uie_dataset(path, SCHEMA)


if __name__ == "__main__":
    unittest.main()
