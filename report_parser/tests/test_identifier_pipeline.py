from __future__ import annotations

import io
import unittest
from dataclasses import replace

from PIL import Image

from rk3588_report_parser.identifier_linker import BatchLinkOutcome
from rk3588_report_parser.identifier_models import ClassifiedCandidate
from rk3588_report_parser.identifier_pipeline import IdentifierParser
from rk3588_report_parser.identifier_rules import parse_identifier_rule_settings
from rk3588_report_parser.settings import (
    LlmSettings,
    OcrSettings,
    ParserSettings,
    QualitySettings,
    ValidationSettings,
)


def settings():
    return ParserSettings(
        OcrSettings("http://127.0.0.1:5002/ocr", 5),
        LlmSettings("http://127.0.0.1:8010/v1/chat/completions", "test", 5, 256),
        QualitySettings(100, 1, 1, 2, 0.5),
        ValidationSettings(2, False, True),
    )


class FakeLinker:
    def link(self, candidates, llm_settings):
        selected = []
        mapping = {"患者ID": "patient_id", "住院号": "inpatient_no", "申请单号": "exam_request_no"}
        used = set()
        for candidate in candidates:
            identifier_type = mapping.get(candidate.raw_label)
            if identifier_type and identifier_type not in used:
                used.add(identifier_type)
                selected.append(ClassifiedCandidate(candidate, identifier_type, True))
        return BatchLinkOutcome(tuple(selected), 1.0, 1.0, "{}", "{}")


class UnexpectedModelLinker:
    def link(self, candidates, llm_settings, allowed_types_by_id=None):
        raise AssertionError("unique configured rules must not call the model")


class FixedOcrClient:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def recognize(self, image_bytes, ocr_settings):
        self.calls += 1
        return self.response


class SequenceOcrClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def recognize(self, image_bytes, ocr_settings):
        with Image.open(io.BytesIO(image_bytes)) as image:
            self.calls.append(image.size)
        if not self.responses:
            return {"ok": True, "ocr": []}
        return self.responses.pop(0)


class IdentifierPipelineTests(unittest.TestCase):
    def test_rule_profile_accepts_an_unlabeled_unique_length(self) -> None:
        configured = replace(
            settings(),
            identifier_rules=parse_identifier_rule_settings(
                {
                    "enabled": True,
                    "profile": "hospital-a",
                    "fields": [
                        {
                            "type": "patient_id",
                            "lengths": [11],
                            "charset": "digits",
                            "priority": 100,
                            "allow_unlabeled": True,
                        }
                    ],
                }
            ),
        )
        response = {
            "ok": True,
            "ocr": [
                {"text": "申请信息", "score": 0.99, "box": [50, 50, 180, 90]},
                {"text": "60017095179", "score": 0.99, "box": [700, 300, 930, 340]},
            ],
        }

        outcome = IdentifierParser(configured, linker=UnexpectedModelLinker()).parse_ocr_response(
            response, (1000, 1200)
        )

        self.assertEqual(outcome.result.status, "accepted")
        self.assertEqual(outcome.result.primary_identifier.value, "60017095179")
        self.assertEqual(outcome.result.identifiers[0].decision_source, "configured_rule")

    def test_rule_profile_retains_unmatched_number_for_review(self) -> None:
        configured = replace(
            settings(),
            identifier_rules=parse_identifier_rule_settings(
                {
                    "enabled": True,
                    "profile": "hospital-a",
                    "fields": [
                        {"type": "patient_id", "lengths": [12], "charset": "digits", "priority": 100}
                    ],
                }
            ),
        )
        response = {
            "ok": True,
            "ocr": [
                {"text": "申请信息", "score": 0.99, "box": [50, 50, 180, 90]},
                {"text": "60017095179", "score": 0.99, "box": [700, 300, 930, 340]},
            ],
        }

        outcome = IdentifierParser(configured, linker=UnexpectedModelLinker()).parse_ocr_response(
            response, (1000, 1200)
        )

        self.assertEqual(outcome.result.status, "review_required")
        self.assertEqual(outcome.result.alternatives[0].type, "unknown_identifier")
        self.assertEqual(outcome.result.alternatives[0].value, "60017095179")

    def test_rule_profile_splits_joined_values_and_keeps_all_matches_without_model(self) -> None:
        configured = replace(
            settings(),
            identifier_rules=parse_identifier_rule_settings(
                {
                    "enabled": True,
                    "profile": "ocr-only",
                    "fields": [
                        {
                            "type": "exam_request_no",
                            "lengths": [12],
                            "charset": "alphanumeric",
                            "prefixes": ["01D"],
                            "priority": 100,
                        },
                        {
                            "type": "imaging_no",
                            "lengths": [11],
                            "charset": "digits",
                            "priority": 90,
                            "allow_unlabeled": True,
                        },
                    ],
                }
            ),
        )
        response = {
            "ok": True,
            "ocr": [
                {"text": "申请号 影像号", "score": 0.99, "box": [50, 50, 300, 90]},
                {"text": "01D11555114532607105741", "score": 0.99, "box": [500, 100, 950, 140]},
                {"text": "01D115551150 32607105717", "score": 0.99, "box": [500, 160, 950, 200]},
            ],
        }

        outcome = IdentifierParser(configured, linker=UnexpectedModelLinker()).parse_ocr_response(
            response, (1000, 1200)
        )

        self.assertEqual(outcome.result.status, "accepted")
        self.assertEqual(outcome.result.engine["model"], "disabled")
        self.assertEqual(outcome.result.timings["classification_ms"], 0.0)
        self.assertEqual(outcome.result.primary_identifier.value, "01D115551145")
        self.assertEqual(
            {(item.type, item.value) for item in outcome.result.identifiers},
            {
                ("exam_request_no", "01D115551145"),
                ("exam_request_no", "01D115551150"),
                ("imaging_no", "32607105741"),
                ("imaging_no", "32607105717"),
            },
        )

    def test_single_length_profile_returns_one_generic_identifier(self) -> None:
        configured = replace(
            settings(),
            identifier_rules=parse_identifier_rule_settings(
                {
                    "enabled": True,
                    "profile": "single-length",
                    "fields": [
                        {
                            "type": "selected_identifier",
                            "lengths": [8],
                            "charset": "alphanumeric",
                            "allow_unlabeled": True,
                            "priority": 1000,
                        }
                    ],
                }
            ),
        )
        response = {
            "ok": True,
            "ocr": [
                {"text": "同济医院体检单", "score": 0.99, "box": [50, 50, 300, 90]},
                {"text": "检验/放射科P2540558", "score": 0.99, "box": [50, 110, 350, 150]},
                {"text": "01D115551153", "score": 0.99, "box": [500, 300, 750, 340]},
            ],
        }

        outcome = IdentifierParser(configured, linker=UnexpectedModelLinker()).parse_ocr_response(
            response, (1000, 1200)
        )
        payload = outcome.result.to_dict()

        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["identifier"], "P2540558")
        self.assertEqual(payload["primary_identifier"]["type"], "selected_identifier")
        self.assertEqual(len(payload["identifiers"]), 1)

    def test_single_length_profile_refuses_multiple_matches(self) -> None:
        configured = replace(
            settings(),
            identifier_rules=parse_identifier_rule_settings(
                {
                    "enabled": True,
                    "profile": "single-length",
                    "fields": [
                        {
                            "type": "selected_identifier",
                            "lengths": [8],
                            "charset": "alphanumeric",
                            "allow_unlabeled": True,
                            "priority": 1000,
                        }
                    ],
                }
            ),
        )
        response = {
            "ok": True,
            "ocr": [
                {"text": "申请信息", "score": 0.99, "box": [50, 50, 200, 90]},
                {"text": "A1234567", "score": 0.99, "box": [50, 110, 250, 150]},
                {"text": "B1234567", "score": 0.98, "box": [50, 170, 250, 210]},
            ],
        }

        outcome = IdentifierParser(configured, linker=UnexpectedModelLinker()).parse_ocr_response(
            response, (1000, 1200)
        )
        payload = outcome.result.to_dict()

        self.assertEqual(payload["status"], "review_required")
        self.assertIsNone(payload["identifier"])
        self.assertEqual(len(payload["alternatives"]), 2)
        self.assertIn("multiple_target_identifiers", payload["review_reasons"])

    def test_single_length_profile_uses_count_despite_quality_and_confidence_warnings(self) -> None:
        configured = replace(
            settings(),
            quality=QualitySettings(1600, 50, 5000, 5, 0.95),
            identifier_rules=parse_identifier_rule_settings(
                {
                    "enabled": True,
                    "profile": "count-only",
                    "fields": [
                        {
                            "type": "selected_identifier",
                            "lengths": [11],
                            "charset": "alphanumeric",
                            "allow_unlabeled": True,
                            "priority": 1000,
                        }
                    ],
                }
            ),
        )
        client = FixedOcrClient(
            {
                "ok": True,
                "ocr": [
                    {"text": "病人ID：13800138000", "score": 0.20, "box": [20, 20, 280, 60]},
                ],
            }
        )
        image = Image.new("RGB", (320, 240), "white")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG")

        outcome = IdentifierParser(
            configured,
            ocr_client=client,
            linker=UnexpectedModelLinker(),
        ).parse_bytes(buffer.getvalue())
        payload = outcome.result.to_dict()

        self.assertEqual(client.calls, 1)
        self.assertFalse(payload["quality"]["ok"])
        self.assertIn("insufficient_resolution", payload["quality"]["reasons"])
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["identifier"], "13800138000")

    def test_single_length_profile_refines_a_partial_ocr_row(self) -> None:
        configured = replace(
            settings(),
            identifier_rules=parse_identifier_rule_settings(
                {
                    "enabled": True,
                    "profile": "count-only",
                    "fields": [
                        {
                            "type": "selected_identifier",
                            "lengths": [16],
                            "charset": "alphanumeric",
                            "allow_unlabeled": True,
                            "priority": 1000,
                        }
                    ],
                }
            ),
        )
        client = SequenceOcrClient(
            [
                {
                    "ok": True,
                    "ocr": [
                        {"text": "处方/申请号", "score": 0.99, "box": [25, 720, 180, 755]},
                        {"text": "1D20260730", "score": 0.70, "box": [30, 780, 200, 805]},
                    ],
                },
                {
                    "ok": True,
                    "ocr": [
                        {"text": "01D2026073013779", "score": 0.71, "box": [80, 160, 1160, 250]},
                    ],
                },
            ]
        )
        image = Image.new("RGB", (650, 1920), "white")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG")

        outcome = IdentifierParser(
            configured,
            ocr_client=client,
            linker=UnexpectedModelLinker(),
        ).parse_bytes(buffer.getvalue())
        payload = outcome.result.to_dict()

        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["identifier"], "01D2026073013779")
        self.assertEqual(len(client.calls), 2)
        self.assertGreater(client.calls[1][0], 650)
        self.assertEqual(payload["ocr_summary"]["refinement"]["mode"], "candidate_regions")
        self.assertEqual(payload["ocr_summary"]["refinement"]["recovered_values"], 1)

    def test_extracts_multiple_identifiers_with_one_primary(self) -> None:
        response = {
            "ok": True,
            "ocr": [
                {"text": "患者ID", "score": 0.99, "box": [50, 50, 180, 90]},
                {"text": "P20260001", "score": 0.99, "box": [220, 50, 430, 90]},
                {"text": "住院号", "score": 0.99, "box": [50, 130, 180, 170]},
                {"text": "ZY20260001", "score": 0.99, "box": [220, 130, 430, 170]},
                {"text": "申请单号", "score": 0.99, "box": [50, 210, 180, 250]},
                {"text": "SQ20260001", "score": 0.99, "box": [220, 210, 430, 250]},
            ],
        }
        outcome = IdentifierParser(settings(), linker=FakeLinker()).parse_ocr_response(response, (1000, 1200))
        payload = outcome.result.to_dict()

        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["primary_identifier"]["type"], "patient_id")
        self.assertEqual(len(payload["identifiers"]), 3)
        for item in payload["identifiers"]:
            self.assertTrue(item["value_span_ids"])

    def test_no_number_candidates_rejects_before_model(self) -> None:
        response = {
            "ok": True,
            "ocr": [
                {"text": "患者姓名", "score": 0.99, "box": [50, 50, 180, 90]},
                {"text": "张三", "score": 0.99, "box": [220, 50, 300, 90]},
            ],
        }
        outcome = IdentifierParser(settings(), linker=FakeLinker()).parse_ocr_response(response, (1000, 1200))
        self.assertEqual(outcome.result.status, "rejected")
        self.assertIn("no_identifier_candidates", outcome.result.rejection_reasons)


if __name__ == "__main__":
    unittest.main()
