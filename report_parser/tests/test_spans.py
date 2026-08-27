from __future__ import annotations

import unittest

from rk3588_report_parser.spans import build_spans


class SpanTests(unittest.TestCase):
    def test_build_spans_keeps_atomic_items_and_reading_order(self) -> None:
        response = {
            "ocr": [
                {"text": "张三", "score": 0.97, "box": [180, 40, 240, 70]},
                {"text": "患者ID", "score": 0.99, "box": [10, 100, 100, 130]},
                {"text": "姓名", "score": 0.98, "box": [10, 40, 70, 70]},
                {"text": "P2605260007", "score": 0.96, "box": [120, 100, 300, 130]},
            ]
        }

        spans = build_spans(response, (400, 200))

        self.assertEqual([span.id for span in spans], [1, 2, 3, 4])
        self.assertEqual([span.text for span in spans], ["姓名", "张三", "患者ID", "P2605260007"])
        self.assertEqual([span.line_id for span in spans], [1, 1, 2, 2])
        self.assertEqual(spans[0].normalized_box, (25, 200, 175, 350))
        self.assertEqual(spans[3].normalized_box, (300, 500, 750, 650))

    def test_spans_without_box_are_excluded(self) -> None:
        response = {"ocr": [{"text": "姓名", "score": 0.99}, {"text": "张三", "box": [1, 1, 20, 20]}]}
        spans = build_spans(response, (100, 100))
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].text, "张三")


if __name__ == "__main__":
    unittest.main()
