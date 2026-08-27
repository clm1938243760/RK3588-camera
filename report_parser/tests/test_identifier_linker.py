from __future__ import annotations

import json
import unittest

from rk3588_report_parser.identifier_linker import (
    BatchIdentifierLinker,
    explicit_identifier_type,
    parse_classifications,
    parse_verifications,
)
from rk3588_report_parser.identifier_models import IdentifierCandidate
from rk3588_report_parser.prompt import ModelResponseError
from rk3588_report_parser.settings import LlmSettings


def candidate(candidate_id=1, raw_label="患者ID"):
    return IdentifierCandidate(
        candidate_id,
        raw_label,
        "60017768119",
        (1,),
        (2,),
        "full_span",
        "same_line_right",
        0.02,
        0.99,
        2,
        (10, 10, 100, 40),
        ((120, 10, 260, 40),),
    )


class FakeBatchChat:
    def __init__(self):
        self.calls = []

    def link(self, system_prompt, user_prompt, settings):
        payload = json.loads(user_prompt)
        self.calls.append(payload)
        if "candidates" in payload:
            return '{"classifications":[{"candidate_id":1,"type":"patient_id"}]}'
        return '{"confirmed_candidate_ids":[1]}'


class IdentifierLinkerTests(unittest.TestCase):
    def test_explicit_medical_labels_have_deterministic_types(self) -> None:
        self.assertEqual(explicit_identifier_type("ID"), "patient_id")
        self.assertEqual(explicit_identifier_type("卡号："), "medical_card_no")
        self.assertEqual(explicit_identifier_type("处方/申请号："), "exam_request_no")
        self.assertEqual(explicit_identifier_type("检查号"), "exam_no")
        self.assertEqual(explicit_identifier_type("影像号"), "imaging_no")
        self.assertIsNone(explicit_identifier_type("序号"))

    def test_runs_two_batch_calls_and_never_returns_free_text(self) -> None:
        client = FakeBatchChat()
        settings = LlmSettings("http://127.0.0.1:8010/v1/chat/completions", "test", 5, 256)
        outcome = BatchIdentifierLinker(client).link([candidate()], settings)

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(outcome.candidates[0].identifier_type, "patient_id")
        self.assertTrue(outcome.candidates[0].confirmed)
        self.assertEqual(outcome.candidates[0].candidate.value, "60017768119")

    def test_explicit_label_overrides_small_model_type(self) -> None:
        settings = LlmSettings("http://127.0.0.1:8010/v1/chat/completions", "test", 5, 256)
        outcome = BatchIdentifierLinker(FakeBatchChat()).link([candidate(raw_label="卡号")], settings)

        self.assertEqual(outcome.candidates[0].identifier_type, "medical_card_no")

    def test_rejects_unknown_ids_types_and_invalid_verifications(self) -> None:
        with self.assertRaises(ModelResponseError):
            parse_classifications('{"classifications":[{"candidate_id":9,"type":"patient_id"}]}', [1])
        with self.assertRaises(ModelResponseError):
            parse_classifications('{"classifications":[{"candidate_id":1,"type":"phone"}]}', [1])
        with self.assertRaises(ModelResponseError):
            parse_verifications('{"confirmed_candidate_ids":[9]}', {1: "patient_id"})

    def test_omitted_verification_ids_are_rejected_by_the_model(self) -> None:
        self.assertEqual(
            parse_verifications('{"confirmed_candidate_ids":[]}', {1: "patient_id"}),
            {1: False},
        )


if __name__ == "__main__":
    unittest.main()
