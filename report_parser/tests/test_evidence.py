from __future__ import annotations

import unittest

from rk3588_report_parser.evidence import validate_model_evidence
from rk3588_report_parser.models import FIELD_NAMES, OcrSpan
from rk3588_report_parser.settings import ValidationSettings
from rk3588_report_parser.validation import materialize_and_validate


def span(span_id: int, line_id: int, text: str, left: int = 20) -> OcrSpan:
    return OcrSpan(
        id=span_id,
        source_index=span_id,
        line_id=line_id,
        text=text,
        box=(left, line_id * 40, left + 180, line_id * 40 + 25),
        normalized_box=(left, line_id * 40, left + 180, line_id * 40 + 25),
        score=0.98,
    )


def empty_links():
    return {field: [] for field in FIELD_NAMES}


def full_modes():
    return {field: "full_span" for field in FIELD_NAMES}


class EvidenceValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = ValidationSettings(
            max_age_delta_years=2,
            require_patient_name=False,
            require_identifier=False,
        )

    def test_combined_label_and_value_uses_after_delimiter_evidence(self) -> None:
        spans = [span(1, 1, "ID:60017768119")]
        values, labels, modes = empty_links(), empty_links(), full_modes()
        values["patient_id"] = [1]
        labels["patient_id"] = [1]
        modes["patient_id"] = "after_delimiter"

        evidence = validate_model_evidence(values, labels, modes, spans)
        fields, reasons = materialize_and_validate(
            evidence.value_links,
            spans,
            self.settings,
            label_links=evidence.label_links,
            value_modes=evidence.value_modes,
        )

        self.assertEqual(evidence.reasons, [])
        self.assertEqual(reasons, [])
        self.assertEqual(fields["patient_id"].value, "60017768119")
        self.assertEqual(fields["patient_id"].label_span_ids, [1])

    def test_separate_label_can_reference_contiguous_multiline_value(self) -> None:
        spans = [
            span(1, 1, "Exam Item"),
            span(2, 2, "MRI right wrist"),
            span(3, 3, "MRI left knee"),
        ]
        values, labels, modes = empty_links(), empty_links(), full_modes()
        values["exam_item"] = [2, 3]
        labels["exam_item"] = [1]

        evidence = validate_model_evidence(values, labels, modes, spans)

        self.assertEqual(evidence.reasons, [])
        self.assertEqual(evidence.value_links["exam_item"], [2, 3])

    def test_value_without_label_is_rejected(self) -> None:
        spans = [span(1, 1, "60017768119")]
        values, labels, modes = empty_links(), empty_links(), full_modes()
        values["patient_id"] = [1]

        evidence = validate_model_evidence(values, labels, modes, spans)

        self.assertEqual(evidence.value_links["patient_id"], [])
        self.assertIn("patient_id:model_evidence_requires_one_label", evidence.reasons)


if __name__ == "__main__":
    unittest.main()
