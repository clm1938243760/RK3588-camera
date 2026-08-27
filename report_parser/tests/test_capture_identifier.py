from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from rk3588_report_parser.models import OcrSpan
from rk3588_report_parser.capture_identifier import (
    CaptureIdentifierExtraction,
    CaptureIdentifierExtractor,
    decide_capture_retry,
    load_capture_parser_settings,
    verify_capture_pair,
)


class FakeParser:
    def __init__(self, result=None, error=None, spans=()) -> None:
        self.result = result
        self.error = error
        self.spans = spans

    def parse_bytes(self, image_bytes: bytes):
        if image_bytes != b"jpeg":
            raise AssertionError("unexpected image")
        if self.error is not None:
            raise self.error
        return SimpleNamespace(result=self.result, spans=self.spans)


def result(status, value=None, review=None, rejection=None, alternatives=0):
    primary = None
    identifiers = []
    if value is not None:
        primary = SimpleNamespace(type="selected_identifier", value=value)
        identifiers = [primary]
    return SimpleNamespace(
        status=status,
        primary_identifier=primary,
        identifiers=identifiers,
        alternatives=[object()] * alternatives,
        review_reasons=review or [],
        rejection_reasons=rejection or [],
        ocr_summary={"item_count": 12},
        quality=SimpleNamespace(image_size=(800, 600)),
    )


class CaptureIdentifierExtractorTests(unittest.TestCase):
    def test_accepts_one_selected_identifier_without_exposing_value_in_status(self) -> None:
        extraction = CaptureIdentifierExtractor(
            FakeParser(result("accepted", "03D2026072802066"))
        ).extract(b"jpeg")

        self.assertTrue(extraction.accepted)
        self.assertEqual(extraction.value, "03D2026072802066")
        self.assertNotIn("03D2026072802066", json.dumps(extraction.public_status()))
        self.assertTrue(extraction.public_status()["value_available"])

    def test_review_result_does_not_produce_field_a(self) -> None:
        extraction = CaptureIdentifierExtractor(
            FakeParser(
                result(
                    "review_required",
                    review=["multiple_target_identifiers"],
                    alternatives=2,
                )
            )
        ).extract(b"jpeg")

        self.assertFalse(extraction.accepted)
        self.assertIsNone(extraction.value)
        self.assertEqual(extraction.status, "review_required")
        self.assertEqual(extraction.alternative_count, 2)

    def test_keeps_full_ocr_document_private_while_status_only_exposes_counts(self) -> None:
        patient_text = "患者姓名张三"
        extraction = CaptureIdentifierExtractor(
            FakeParser(
                result("accepted", "03D2026072802066"),
                spans=(
                    OcrSpan(1, 0, 1, patient_text, (10, 20, 150, 50), (12, 33, 188, 83), 0.96),
                ),
            )
        ).extract(b"jpeg")

        self.assertIsNotNone(extraction.document)
        self.assertEqual(extraction.document.full_text, patient_text)
        public = json.dumps(extraction.public_status(), ensure_ascii=False)
        self.assertNotIn(patient_text, public)
        self.assertEqual(extraction.public_status()["full_text"]["item_count"], 1)

    def test_two_pass_requires_an_exact_match_and_then_retries(self) -> None:
        common = {
            "parser_status": "accepted",
            "identifier_count": 1,
            "alternative_count": 0,
            "ocr_item_count": 1,
            "elapsed_ms": 10.0,
        }
        field_a = CaptureIdentifierExtraction(status="accepted", value="A1234567", **common)
        same_b = CaptureIdentifierExtraction(status="accepted", value="A1234567", **common)
        other_b = CaptureIdentifierExtraction(status="accepted", value="B1234567", **common)

        accepted = verify_capture_pair(field_a, same_b, attempt=1, max_attempts=2)
        retrying = verify_capture_pair(field_a, other_b, attempt=1, max_attempts=3)
        retrying_again = verify_capture_pair(field_a, other_b, attempt=2, max_attempts=3)
        rejected = verify_capture_pair(field_a, other_b, attempt=3, max_attempts=3)

        self.assertTrue(accepted.accepted)
        self.assertEqual(accepted.reason, "exact_match")
        self.assertEqual(retrying.status, "retrying")
        self.assertEqual(retrying.attempt, 2)
        self.assertEqual(retrying_again.status, "retrying")
        self.assertEqual(retrying_again.attempt, 3)
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(rejected.reason, "identifier_mismatch")

    def test_parser_exception_becomes_sanitized_error(self) -> None:
        extraction = CaptureIdentifierExtractor(
            FakeParser(error=RuntimeError("patient 03D2026072802066"))
        ).extract(b"jpeg")

        self.assertEqual(extraction.status, "error")
        self.assertEqual(extraction.reasons, ("parser_error:RuntimeError",))
        self.assertNotIn("03D2026072802066", json.dumps(extraction.public_status()))

    def test_ambiguous_first_field_retries_before_final_rejection(self) -> None:
        retrying = decide_capture_retry("field_a_review_required", 1, 3)
        retrying_again = decide_capture_retry("field_a_review_required", 2, 3)
        rejected = decide_capture_retry("field_a_review_required", 3, 3)

        self.assertEqual(retrying.public_status(), {
            "status": "retrying",
            "reason": "field_a_review_required",
            "attempt": 2,
        })
        self.assertEqual(retrying_again.status, "retrying")
        self.assertEqual(retrying_again.attempt, 3)
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(rejected.attempt, 3)

    def test_loads_active_single_length_rules_and_rejects_general_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            rules = root / "rules.json"
            config.write_text(
                json.dumps(
                    {
                        "identifier_rules": {
                            "enabled": False,
                            "profile": "off",
                            "fields": [],
                        }
                    }
                ),
                encoding="utf-8",
            )
            rules.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "profile": "single-length",
                        "fields": [
                            {
                                "type": "selected_identifier",
                                "lengths": [16],
                                "charset": "alphanumeric",
                                "allow_unlabeled": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            settings = load_capture_parser_settings(
                config,
                rules,
                "http://127.0.0.1:5002/ocr",
                12.0,
            )
            self.assertEqual(settings.identifier_rules.fields[0].lengths, (16,))
            self.assertEqual(settings.ocr.timeout_seconds, 12.0)

            rules.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "profile": "general",
                        "fields": [
                            {
                                "type": "patient_id",
                                "lengths": [11],
                                "charset": "digits",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "selected_identifier"):
                load_capture_parser_settings(
                    config,
                    rules,
                    "http://127.0.0.1:5002/ocr",
                    12.0,
                )


if __name__ == "__main__":
    unittest.main()
