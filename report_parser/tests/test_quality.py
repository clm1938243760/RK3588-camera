from __future__ import annotations

import unittest

from PIL import Image, ImageDraw

from rk3588_report_parser.quality import assess_image
from rk3588_report_parser.settings import QualitySettings


SETTINGS = QualitySettings(
    min_longest_side=1600,
    min_contrast=6.0,
    min_laplacian_energy=25.0,
    min_ocr_items=3,
    min_ocr_score=0.65,
)


class QualityTests(unittest.TestCase):
    def test_solid_white_image_is_rejected(self) -> None:
        image = Image.new("RGB", (1800, 2000), "white")
        assessment = assess_image(image, SETTINGS)
        self.assertFalse(assessment.ok)
        self.assertIn("low_contrast", assessment.reasons)
        self.assertIn("blurry", assessment.reasons)

    def test_document_like_lines_pass_quality_gate(self) -> None:
        image = Image.new("RGB", (1800, 2000), "white")
        draw = ImageDraw.Draw(image)
        for y in range(120, 1880, 45):
            draw.rectangle((120, y, 1580, y + 9), fill="black")
        assessment = assess_image(image, SETTINGS)
        self.assertTrue(assessment.ok, assessment.reasons)
        self.assertGreater(assessment.metrics["laplacian_energy"], 25.0)


if __name__ == "__main__":
    unittest.main()
