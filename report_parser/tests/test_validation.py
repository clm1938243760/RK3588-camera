from __future__ import annotations

import datetime as dt
import unittest

from rk3588_report_parser.models import FIELD_NAMES, OcrSpan
from rk3588_report_parser.settings import ValidationSettings
from rk3588_report_parser.validation import is_plausible_field_value, materialize_and_validate


def span(span_id: int, text: str, x: int = 10) -> OcrSpan:
    return OcrSpan(
        id=span_id,
        source_index=span_id,
        line_id=span_id,
        text=text,
        box=(x, 10, x + 80, 30),
        normalized_box=(x, 10, x + 80, 30),
        score=0.98,
    )


def base_links():
    return {field: [] for field in FIELD_NAMES}


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = ValidationSettings(max_age_delta_years=2, require_patient_name=True, require_identifier=True)

    def test_valid_evidence_backed_record(self) -> None:
        spans = [
            span(1, "张三"),
            span(2, "P2605260007"),
            span(3, "男"),
            span(4, "45岁"),
            span(5, "1981-02-03"),
            span(6, "腹部超声"),
            span(7, "R202608100001"),
            span(8, "2026-08-10"),
        ]
        links = base_links()
        links.update({
            "patient_name": [1],
            "patient_id": [2],
            "sex": [3],
            "age": [4],
            "birthday": [5],
            "exam_item": [6],
            "report_no": [7],
            "report_date": [8],
        })

        fields, reasons = materialize_and_validate(links, spans, self.settings, today=dt.date(2026, 8, 10))

        self.assertEqual(reasons, [])
        self.assertEqual(fields["patient_name"].value, "张三")
        self.assertEqual(fields["sex"].value, "男")
        self.assertEqual(fields["age"].value, "45")
        self.assertEqual(fields["birthday"].value, "1981-02-03")
        self.assertEqual(fields["report_date"].value, "2026-08-10")

    def test_unknown_span_and_reused_span_rejects(self) -> None:
        spans = [span(1, "张三"), span(2, "P2605260007")]
        links = base_links()
        links.update({"patient_name": [1], "patient_id": [2], "report_no": [2], "sex": [99]})

        _, reasons = materialize_and_validate(links, spans, self.settings)

        self.assertTrue(any(reason.startswith("report_no:span_reused_by") for reason in reasons))
        self.assertIn("sex:unknown_span_id:99", reasons)

    def test_age_birthday_conflict_rejects(self) -> None:
        spans = [span(1, "张三"), span(2, "P2605260007"), span(3, "10岁"), span(4, "1981-02-03")]
        links = base_links()
        links.update({"patient_name": [1], "patient_id": [2], "age": [3], "birthday": [4]})

        fields, reasons = materialize_and_validate(links, spans, self.settings, today=dt.date(2026, 8, 10))

        self.assertIn("age_birthday_conflict", reasons)
        self.assertEqual(fields["age"].value, "")
        self.assertFalse(fields["age"].validation_ok)

    def test_label_cannot_be_patient_name(self) -> None:
        spans = [span(1, "姓名"), span(2, "P2605260007")]
        links = base_links()
        links.update({"patient_name": [1], "patient_id": [2]})

        fields, reasons = materialize_and_validate(links, spans, self.settings)

        self.assertIn("patient_name:label_selected_as_value", reasons)
        self.assertIn("missing_patient_name", reasons)
        self.assertEqual(fields["patient_name"].value, "")

    def test_combined_label_and_value_is_evidence_backed(self) -> None:
        spans = [span(1, "姓名：张三"), span(2, "患者ID：P2605260007")]
        links = base_links()
        links.update({"patient_name": [1], "patient_id": [2]})

        fields, reasons = materialize_and_validate(links, spans, self.settings)

        self.assertEqual(reasons, [])
        self.assertEqual(fields["patient_name"].value, "张三")
        self.assertEqual(fields["patient_id"].value, "P2605260007")

    def test_candidate_plausibility_reuses_the_final_field_rules(self) -> None:
        self.assertTrue(is_plausible_field_value("patient_name", "Alice"))
        self.assertTrue(is_plausible_field_value("patient_id", "P2605260007"))
        self.assertTrue(is_plausible_field_value("sex", "male"))
        self.assertTrue(is_plausible_field_value("age", "45"))
        self.assertTrue(is_plausible_field_value("birthday", "1981-02-03"))
        self.assertFalse(is_plausible_field_value("patient_name", "Name"))
        self.assertFalse(is_plausible_field_value("age", "forty five"))
        self.assertFalse(is_plausible_field_value("report_date", "not-a-date"))


if __name__ == "__main__":
    unittest.main()
