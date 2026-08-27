from __future__ import annotations

import json
import unittest

from rk3588_report_parser.choice_linker import ConstrainedChoiceLinker
from rk3588_report_parser.models import OcrSpan
from rk3588_report_parser.prompt import ModelResponseError, build_user_prompt, parse_field_links
from rk3588_report_parser.settings import LlmSettings


def span(span_id, line_id, text, left):
    return OcrSpan(
        id=span_id,
        source_index=span_id,
        line_id=line_id,
        text=text,
        box=(left, line_id * 40, left + 80, line_id * 40 + 20),
        normalized_box=(0, 0, 0, 0),
        score=0.99,
    )


class FakeChoiceClient:
    def __init__(self, choices):
        self.choices = choices
        self.calls = []

    def select(self, system_prompt, user_prompt, settings, allowed_ids):
        payload = json.loads(user_prompt)
        field = payload["field"]
        self.calls.append((field, list(allowed_ids), payload))
        return self.choices[field]


def settings():
    return LlmSettings(
        endpoint="http://127.0.0.1:8010/v1/chat/completions",
        model="desktop-test",
        timeout_seconds=5,
        max_tokens=256,
    )


class ConstrainedChoiceLinkerTests(unittest.TestCase):
    def test_returns_only_existing_span_ids_and_removes_selected_ids(self) -> None:
        spans = [
            span(1, 1, "Name", 20),
            span(2, 1, "Alice", 140),
            span(3, 2, "Patient ID", 20),
            span(4, 2, "P2605260007", 180),
            span(5, 3, "Sex", 20),
            span(6, 3, "male", 140),
        ]
        choices = {
            "patient_name": 2,
            "patient_id": 4,
            "sex": 6,
            "age": 0,
            "birthday": 0,
            "his_exam_no": 0,
            "report_no": 0,
            "report_date": 0,
            "exam_item": 0,
        }
        client = FakeChoiceClient(choices)
        linker = ConstrainedChoiceLinker(client)

        links = parse_field_links(linker.link("ignored", build_user_prompt(spans), settings()))

        self.assertEqual(links["patient_name"], [2])
        self.assertEqual(links["patient_id"], [4])
        self.assertEqual(links["sex"], [6])
        self.assertEqual(links["exam_item"], [])
        self.assertNotIn(1, client.calls[0][1])
        self.assertEqual(client.calls[0][2]["ocr_context"][0]["id"], 1)
        self.assertNotIn(2, client.calls[1][1])
        self.assertNotIn(4, client.calls[2][1])
        self.assertEqual(
            [call[0] for call in client.calls],
            ["patient_name", "patient_id", "sex"],
        )

    def test_fails_closed_when_the_prompt_has_too_many_spans(self) -> None:
        spans = [span(1, 1, "Name", 20), span(2, 1, "Alice", 140)]
        linker = ConstrainedChoiceLinker(FakeChoiceClient({}), max_candidate_spans=1)

        with self.assertRaises(ModelResponseError):
            linker.link("ignored", build_user_prompt(spans), settings())

    def test_selects_optional_his_identifier_after_report_fields(self) -> None:
        rows = [
            ("Name", "Alice"),
            ("Patient ID", "P2605260007"),
            ("Sex", "male"),
            ("Age", "45"),
            ("Birthday", "1981-02-03"),
            ("Report No", "R202608100001"),
            ("Report Date", "2026-08-10"),
            ("Exam Item", "Abdominal ultrasound"),
            ("HIS Exam No", "E2605260001"),
        ]
        spans = []
        for index, (label, value) in enumerate(rows, start=1):
            spans.append(span(index * 2 - 1, index, label, 20))
            spans.append(span(index * 2, index, value, 180))
        choices = {
            "patient_name": 2,
            "patient_id": 4,
            "sex": 6,
            "age": 8,
            "birthday": 10,
            "report_no": 12,
            "report_date": 14,
            "exam_item": 16,
            "his_exam_no": 18,
        }
        client = FakeChoiceClient(choices)

        links = parse_field_links(ConstrainedChoiceLinker(client).link("ignored", build_user_prompt(spans), settings()))

        self.assertEqual(links["report_no"], [12])
        self.assertEqual(links["report_date"], [14])
        self.assertEqual(links["his_exam_no"], [18])
        self.assertEqual(
            [call[0] for call in client.calls],
            [
                "patient_name",
                "patient_id",
                "sex",
                "age",
                "birthday",
                "report_no",
                "report_date",
                "exam_item",
                "his_exam_no",
            ],
        )

    def test_uses_fixed_links_without_model_calls_and_filters_invalid_candidates(self) -> None:
        spans = [
            span(1, 1, "Name", 20),
            span(2, 1, "Alice", 140),
            span(3, 2, "Patient ID", 20),
            span(4, 2, "P2605260007", 180),
            span(5, 3, "Sex", 20),
            span(6, 3, "male", 140),
            span(7, 4, "Age", 20),
            span(8, 4, "45", 140),
            span(9, 5, "Report No", 20),
            span(10, 5, "R202608100001", 180),
            span(11, 6, "not-valid!", 20),
        ]
        choices = {
            "patient_id": 4,
            "age": 8,
            "report_no": 10,
            "his_exam_no": 0,
            "exam_item": 0,
        }
        client = FakeChoiceClient(choices)
        linker = ConstrainedChoiceLinker(client)

        links = parse_field_links(
            linker.link(
                "ignored",
                build_user_prompt(spans, fixed_links={"patient_name": [2], "sex": [6]}),
                settings(),
            )
        )

        called_fields = [call[0] for call in client.calls]
        self.assertEqual(links["patient_name"], [2])
        self.assertEqual(links["sex"], [6])
        self.assertNotIn("patient_name", called_fields)
        self.assertNotIn("sex", called_fields)
        self.assertIn("patient_id", called_fields)
        patient_id_call = next(call for call in client.calls if call[0] == "patient_id")
        self.assertNotIn(1, patient_id_call[1])
        self.assertNotIn(11, patient_id_call[1])


if __name__ == "__main__":
    unittest.main()
