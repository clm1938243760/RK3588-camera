from __future__ import annotations

import io
import unittest

from PIL import Image

from rk3588_report_parser.capture_orientation import prepare_document_jpeg, rotate_jpeg
from rk3588_report_parser.paper_detector import PaperDetection


def jpeg(width: int = 80, height: int = 40) -> bytes:
    image = Image.new("RGB", (width, height), "white")
    output = io.BytesIO()
    image.save(output, format="JPEG")
    return output.getvalue()


class CaptureOrientationTests(unittest.TestCase):
    def test_zero_rotation_returns_original_bytes(self) -> None:
        source = jpeg()
        self.assertIs(rotate_jpeg(source, 0), source)

    def test_counterclockwise_rotation_swaps_dimensions(self) -> None:
        rotated = rotate_jpeg(jpeg(), 90)
        with Image.open(io.BytesIO(rotated)) as image:
            self.assertEqual(image.size, (40, 80))

    def test_invalid_rotation_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            rotate_jpeg(jpeg(), 45)

    def test_detected_document_is_rectified_rotated_and_upscaled(self) -> None:
        detection = PaperDetection(
            corners=((80.0, 40.0), (720.0, 40.0), (720.0, 360.0), (80.0, 360.0)),
            frame_width=800,
            frame_height=400,
            confidence=0.99,
            inference_ms=5.0,
        )
        prepared = prepare_document_jpeg(
            jpeg(800, 400),
            detection,
            degrees_counterclockwise=90,
            target_long_side=640,
        )
        with Image.open(io.BytesIO(prepared)) as image:
            self.assertEqual(image.size, (320, 640))

    def test_large_document_is_downscaled_to_target_long_side(self) -> None:
        detection = PaperDetection(
            corners=((0.0, 0.0), (1599.0, 0.0), (1599.0, 799.0), (0.0, 799.0)),
            frame_width=1600,
            frame_height=800,
            confidence=0.99,
            inference_ms=5.0,
        )
        prepared = prepare_document_jpeg(
            jpeg(1600, 800),
            detection,
            target_long_side=800,
        )
        with Image.open(io.BytesIO(prepared)) as image:
            self.assertEqual(image.size, (800, 400))
