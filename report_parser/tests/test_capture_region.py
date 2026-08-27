from __future__ import annotations

import io
import unittest

from PIL import Image

from rk3588_report_parser.capture_region import (
    DocumentRecognitionRegion,
    crop_document_jpeg,
    remap_extraction_to_full_document,
)
from rk3588_report_parser.capture_text import FullTextExtraction, build_captured_text_document
from rk3588_report_parser.models import OcrSpan


def span(span_id: int, text: str, box: tuple[int, int, int, int], score: float = 0.95) -> OcrSpan:
    left, top, right, bottom = box
    polygon = ((left, top), (right, top), (right, bottom), (left, bottom))
    return OcrSpan(
        id=span_id,
        source_index=span_id - 1,
        line_id=span_id,
        text=text,
        box=box,
        normalized_box=box,
        score=score,
        polygon=polygon,
        normalized_polygon=polygon,
    )


class DocumentRecognitionRegionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.region = DocumentRecognitionRegion(
            crop_top=0.08,
            crop_bottom=0.68,
            accept_top=0.10,
            accept_bottom=0.66,
        )

    def test_crop_uses_padding_around_the_accepted_region(self) -> None:
        output = io.BytesIO()
        Image.new("RGB", (100, 1000), "white").save(output, format="JPEG")

        cropped = crop_document_jpeg(output.getvalue(), self.region)

        self.assertEqual(cropped.full_size, (100, 1000))
        self.assertEqual(cropped.crop_box, (0, 80, 100, 680))
        self.assertEqual(cropped.accept_box, (0, 100, 100, 660))
        with Image.open(io.BytesIO(cropped.image_bytes)) as image:
            self.assertEqual(image.size, (100, 600))
        self.assertEqual(cropped.region.crop_normalized, (0, 80, 1000, 680))
        self.assertEqual(cropped.region.accept_normalized, (0, 100, 1000, 660))

    def test_remap_filters_padding_and_restores_full_page_coordinates(self) -> None:
        output = io.BytesIO()
        Image.new("RGB", (100, 1000), "white").save(output, format="JPEG")
        cropped = crop_document_jpeg(output.getvalue(), self.region)
        document = build_captured_text_document(
            [
                span(1, "top-padding", (10, 5, 90, 15), 0.20),
                span(2, "patient-id", (10, 20, 90, 60), 0.95),
                span(3, "bottom-padding", (10, 580, 90, 595), 0.20),
            ],
            (100, 600),
        )
        extraction = FullTextExtraction(
            status="review_required",
            document=document,
            reasons=("low_confidence_blocks",),
        )

        mapped = remap_extraction_to_full_document(extraction, cropped)

        self.assertEqual(mapped.status, "accepted")
        self.assertEqual(mapped.reasons, ())
        self.assertEqual(mapped.document.image_size, (100, 1000))
        self.assertEqual(mapped.document.full_text, "patient-id")
        block = mapped.document.spans[0]
        self.assertEqual(block.box, (10, 100, 90, 140))
        self.assertEqual(block.normalized_box, (100, 100, 900, 140))
        self.assertEqual(block.normalized_polygon[0], (100, 100))

    def test_invalid_region_order_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "contained"):
            DocumentRecognitionRegion(
                crop_top=0.10,
                crop_bottom=0.60,
                accept_top=0.08,
                accept_bottom=0.66,
            )


if __name__ == "__main__":
    unittest.main()
