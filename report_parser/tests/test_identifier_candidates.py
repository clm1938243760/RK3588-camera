from __future__ import annotations

import unittest

from rk3588_report_parser.identifier_candidates import build_identifier_candidates, is_identifier_value
from rk3588_report_parser.models import OcrSpan


def span(span_id, line, text, box, score=0.99):
    return OcrSpan(span_id, span_id, line, text, box, box, score)


class IdentifierCandidateTests(unittest.TestCase):
    def test_keeps_an_unlabeled_identifier_candidate(self) -> None:
        candidates = build_identifier_candidates(
            [span(1, 1, "AB202608110001", (200, 200, 480, 240))]
        )

        self.assertTrue(
            any(
                item.value == "AB202608110001"
                and item.raw_label == ""
                and item.relation == "unlabeled"
                for item in candidates
            )
        )

    def test_builds_same_span_and_same_line_candidates(self) -> None:
        spans = [
            span(1, 1, "患者ID:60017768119", (20, 20, 330, 50)),
            span(2, 2, "住院号", (20, 80, 120, 110)),
            span(3, 2, "ZY20260001", (150, 80, 350, 110)),
        ]
        candidates = build_identifier_candidates(spans)

        self.assertTrue(any(item.relation == "same_span" and item.value == "60017768119" for item in candidates))
        self.assertTrue(
            any(
                item.relation == "same_line_right"
                and item.raw_label == "住院号"
                and item.value == "ZY20260001"
                for item in candidates
            )
        )

    def test_excludes_dates_and_explicit_phone_labels(self) -> None:
        spans = [
            span(1, 1, "报告日期", (20, 20, 120, 50)),
            span(2, 1, "2026-08-10", (150, 20, 300, 50)),
            span(3, 2, "联系电话", (20, 80, 120, 110)),
            span(4, 2, "13800138000", (150, 80, 330, 110)),
        ]
        candidates = build_identifier_candidates(spans)

        self.assertFalse(any(item.value == "2026-08-10" for item in candidates))
        self.assertFalse(any(item.raw_label == "联系电话" for item in candidates))
        self.assertFalse(is_identifier_value("2026-08-10"))
        self.assertFalse(is_identifier_value("13800138000"))
        self.assertFalse(is_identifier_value("027-83663679"))


if __name__ == "__main__":
    unittest.main()
