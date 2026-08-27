from __future__ import annotations

import json
import unittest

from rk3588_report_parser.models import FIELD_NAMES
from rk3588_report_parser.models import OcrSpan
from rk3588_report_parser.prompt import ModelResponseError, _adjacent_same_line_pairs, parse_field_links


def response_with(**values):
    payload = {field: {"span_ids": []} for field in FIELD_NAMES}
    for field, span_ids in values.items():
        payload[field] = {"span_ids": span_ids}
    return json.dumps(payload, ensure_ascii=False)


class PromptTests(unittest.TestCase):
    def test_adjacent_same_line_pairs_are_reading_ordered(self) -> None:
        spans = [
            OcrSpan(3, 2, 2, "later", (20, 30, 40, 40), (0, 0, 0, 0), 1.0),
            OcrSpan(1, 0, 1, "label", (10, 10, 30, 20), (0, 0, 0, 0), 1.0),
            OcrSpan(2, 1, 1, "value", (40, 10, 70, 20), (0, 0, 0, 0), 1.0),
        ]

        self.assertEqual(_adjacent_same_line_pairs(spans), [[1, 2]])

    def test_accepts_exact_schema(self) -> None:
        links = parse_field_links(response_with(patient_name=[2], patient_id=[4]))
        self.assertEqual(links["patient_name"], [2])
        self.assertEqual(links["patient_id"], [4])

    def test_accepts_direct_array_schema_and_numeric_string_ids(self) -> None:
        payload = {field: [] for field in FIELD_NAMES}
        payload["patient_name"] = ["2"]
        payload["patient_id"] = [4]

        links = parse_field_links(json.dumps(payload))

        self.assertEqual(links["patient_name"], [2])
        self.assertEqual(links["patient_id"], [4])

    def test_rejects_direct_array_free_text(self) -> None:
        payload = {field: [] for field in FIELD_NAMES}
        payload["patient_name"] = ["Alice"]
        with self.assertRaises(ModelResponseError):
            parse_field_links(json.dumps(payload))

    def test_rejects_unknown_schema_field(self) -> None:
        payload = json.loads(response_with())
        payload["free_text"] = {"span_ids": [1]}
        with self.assertRaises(ModelResponseError):
            parse_field_links(json.dumps(payload))

    def test_rejects_repeated_span_in_a_field(self) -> None:
        with self.assertRaises(ModelResponseError):
            parse_field_links(response_with(patient_name=[2, 2]))

    def test_extracts_json_from_code_fence(self) -> None:
        content = "```json\n" + response_with(patient_name=[2]) + "\n```"
        self.assertEqual(parse_field_links(content)["patient_name"], [2])


if __name__ == "__main__":
    unittest.main()
