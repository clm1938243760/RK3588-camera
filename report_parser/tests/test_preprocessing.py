from __future__ import annotations

import unittest

from PIL import Image, ImageDraw

from rk3588_report_parser.preprocessing import (
    PreprocessSettings,
    prepare_for_ocr,
    restore_ocr_coordinates,
)


class PreprocessingTests(unittest.TestCase):
    def test_confident_document_quadrilateral_is_rectified(self) -> None:
        image = Image.new("RGB", (1000, 1200), "#202523")
        draw = ImageDraw.Draw(image)
        page = [(180, 120), (830, 180), (900, 1080), (120, 1030)]
        draw.polygon(page, fill="white")
        for y in range(280, 900, 80):
            draw.line((250, y, 760, y + 30), fill="black", width=8)

        result = prepare_for_ocr(
            image,
            PreprocessSettings(
                perspective_correction=True,
                min_document_area_ratio=0.2,
                min_confidence=0.7,
                min_output_side=200,
            ),
        )

        self.assertTrue(result.applied)
        self.assertIsNotNone(result.inverse_transform)
        self.assertGreater(min(result.image.size), 500)

    def test_coordinate_restore_maps_boxes_back_to_original_image(self) -> None:
        response = {"ok": True, "ocr": [{"text": "ID:1234", "score": 0.99, "box": [10, 20, 30, 40]}]}
        restored = restore_ocr_coordinates(
            response,
            (1, 0, 100, 0, 1, 200, 0, 0, 1),
            (1000, 1200),
        )

        self.assertEqual(restored["ocr"][0]["box"], [110.0, 220.0, 130.0, 240.0])

    def test_disabled_perspective_correction_keeps_original_image(self) -> None:
        image = Image.new("RGB", (800, 1200), "white")
        result = prepare_for_ocr(image, PreprocessSettings(perspective_correction=False))

        self.assertFalse(result.applied)
        self.assertIs(result.image, image)


if __name__ == "__main__":
    unittest.main()
