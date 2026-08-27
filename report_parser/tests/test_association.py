from __future__ import annotations

import unittest

from rk3588_report_parser.association import high_confidence_label_links, merge_model_and_label_links
from rk3588_report_parser.models import OcrSpan


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


class AssociationTests(unittest.TestCase):
    def test_uses_unique_same_line_label_value_evidence(self) -> None:
        spans = [
            span(1, 1, "Name", 20),
            span(2, 1, "Alice", 150),
            span(3, 2, "Patient ID", 20),
            span(4, 2, "P2605260007", 190),
            span(5, 3, "Report No", 20),
            span(6, 3, "R202608100001", 190),
        ]

        links = high_confidence_label_links(spans)

        self.assertEqual(links["patient_name"], [2])
        self.assertEqual(links["patient_id"], [4])
        self.assertEqual(links["report_no"], [6])
        self.assertNotIn("sex", links)

    def test_omits_ambiguous_repeated_labels(self) -> None:
        spans = [
            span(1, 1, "Name", 20),
            span(2, 1, "Alice", 150),
            span(3, 2, "Name", 20),
            span(4, 2, "Beth", 150),
        ]

        self.assertNotIn("patient_name", high_confidence_label_links(spans))

    def test_label_geometry_overrides_conflicting_model_link(self) -> None:
        merged, fields = merge_model_and_label_links(
            {"patient_name": [1], "patient_id": [4]},
            {"patient_name": [2]},
        )

        self.assertEqual(merged["patient_name"], [2])
        self.assertEqual(merged["patient_id"], [4])
        self.assertEqual(fields, ["patient_name"])


if __name__ == "__main__":
    unittest.main()
