from __future__ import annotations

import json
import unittest

from rk3588_report_parser.evidence_linker import EvidenceChoiceLinker
from rk3588_report_parser.models import OcrSpan
from rk3588_report_parser.prompt import build_user_prompt, parse_field_associations
from rk3588_report_parser.settings import LlmSettings


def span(span_id: int, line_id: int, text: str, left: int = 20) -> OcrSpan:
    return OcrSpan(
        id=span_id,
        source_index=span_id,
        line_id=line_id,
        text=text,
        box=(left, line_id * 40, left + 220, line_id * 40 + 25),
        normalized_box=(left, line_id * 40, left + 220, line_id * 40 + 25),
        score=0.99,
    )


class FakeEvidenceChoiceClient:
    label_choices = {"patient_name": 2, "patient_id": 1, "exam_item": 3}
    value_spans = {"patient_name": [2], "patient_id": [1], "exam_item": [4, 5]}

    def __init__(self) -> None:
        self.calls = []

    def select(self, system_prompt, user_prompt, settings, allowed_ids):
        payload = json.loads(user_prompt)
        self.calls.append(payload)
        field = payload["field"]
        if payload["stage"] == "label":
            return self.label_choices.get(field, 0)
        expected = self.value_spans[field]
        for option in payload["selectable_options"]:
            if option.get("value_span_ids") == expected:
                return option["id"]
        return 0


class EvidenceChoiceLinkerTests(unittest.TestCase):
    def test_selects_combined_and_multiline_model_evidence(self) -> None:
        spans = [
            span(1, 1, "ID:60017768119"),
            span(2, 2, "Name:Alice"),
            span(3, 3, "Exam Item"),
            span(4, 4, "MRI right wrist"),
            span(5, 5, "MRI left knee"),
        ]
        client = FakeEvidenceChoiceClient()
        settings = LlmSettings(
            endpoint="http://127.0.0.1:8010/v1/chat/completions",
            model="desktop-test",
            timeout_seconds=5,
            max_tokens=256,
        )

        response = EvidenceChoiceLinker(client).link("ignored", build_user_prompt(spans), settings)
        associations = parse_field_associations(response)

        self.assertEqual(associations.label_links["patient_id"], [1])
        self.assertEqual(associations.value_links["patient_id"], [1])
        self.assertEqual(associations.value_modes["patient_id"], "after_delimiter")
        self.assertEqual(associations.label_links["exam_item"], [3])
        self.assertEqual(associations.value_links["exam_item"], [4, 5])
        self.assertTrue(any(call["stage"] == "label" for call in client.calls))
        self.assertTrue(any(call["stage"] == "value" for call in client.calls))

    def test_does_not_use_another_combined_field_as_a_free_value(self) -> None:
        linker = EvidenceChoiceLinker(FakeEvidenceChoiceClient())
        spans = [
            span(1, 1, "ID:60017768119"),
            span(2, 1, "Name:Alice", left=300),
        ]
        normalized = [item.to_prompt_dict() for item in spans]

        options = linker._value_options("patient_name", normalized[0], normalized, [])

        self.assertEqual(options, [])


if __name__ == "__main__":
    unittest.main()
