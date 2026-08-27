from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from rk3588_report_parser.identifier_evaluation import (
    IdentifierDatasetError,
    IdentifierEvaluationSample,
    deployment_gate_failures,
    evaluate_identifier_samples,
    load_identifier_dataset,
)


def result(status, identifiers, primary=None):
    values = [SimpleNamespace(type=identifier_type, value=value) for identifier_type, value in identifiers]
    primary_value = None if primary is None else SimpleNamespace(type=primary[0], value=primary[1])
    return SimpleNamespace(result=SimpleNamespace(status=status, identifiers=values, primary_identifier=primary_value))


class FakeParser:
    def __init__(self, outcomes):
        self.outcomes = outcomes

    def parse_ocr_response(self, response, image_size):
        return self.outcomes[response["sample"]]


class IdentifierEvaluationTests(unittest.TestCase):
    def test_metrics_count_false_accepts_and_exact_identifier_pairs(self) -> None:
        samples = [
            IdentifierEvaluationSample(
                "one", {"sample": "one"}, (1000, 1600), "accepted",
                (("patient_id", "P1234"),), ("patient_id", "P1234"),
            ),
            IdentifierEvaluationSample(
                "two", {"sample": "two"}, (1000, 1600), "rejected", (), None,
            ),
        ]
        parser = FakeParser(
            {
                "one": result("accepted", [("patient_id", "P1234")], ("patient_id", "P1234")),
                "two": result("accepted", [("exam_no", "E9999")], ("exam_no", "E9999")),
            }
        )

        report = evaluate_identifier_samples(samples, parser)

        self.assertEqual(report["false_accepted_samples"], 1)
        self.assertEqual(report["metrics"]["accepted_identifier_precision"], 0.5)
        self.assertEqual(report["metrics"]["primary_identifier_accuracy"], 1.0)
        self.assertTrue(deployment_gate_failures(report))

    def test_dataset_is_deidentified_and_primary_must_be_selected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = root / "valid.json"
            valid.write_text(
                json.dumps(
                    [
                        {
                            "id": "sample-001",
                            "image_size": [1080, 1920],
                            "ocr": {"ok": True, "ocr": []},
                            "expected_status": "accepted",
                            "expected_identifiers": [{"type": "patient_id", "value": "P1234"}],
                            "expected_primary": {"type": "patient_id", "value": "P1234"},
                        }
                    ]
                ),
                encoding="utf-8",
            )
            samples, digest = load_identifier_dataset(valid)
            self.assertEqual(samples[0].expected_primary, ("patient_id", "P1234"))
            self.assertEqual(len(digest), 64)

            invalid = root / "invalid.json"
            invalid.write_text(
                json.dumps(
                    [
                        {
                            "id": "sample-002",
                            "image_path": "sensitive.jpg",
                            "image_size": [1080, 1920],
                            "ocr": {"ok": True, "ocr": []},
                            "expected_status": "rejected",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaises(IdentifierDatasetError):
                load_identifier_dataset(invalid)


if __name__ == "__main__":
    unittest.main()
