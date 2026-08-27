from __future__ import annotations

import unittest

from rk3588_report_parser.capture_text import (
    FullTextExtractor,
    TextRefinementSettings,
    _merge_items,
    _primary_tiles,
    build_captured_text_document,
)
from rk3588_report_parser.models import OcrSpan
from rk3588_report_parser.settings import OcrSettings


def span(span_id, line_id, text, box, score=0.9):
    return OcrSpan(
        id=span_id,
        source_index=span_id - 1,
        line_id=line_id,
        text=text,
        box=box,
        normalized_box=box,
        score=score,
    )


class CapturedTextDocumentTests(unittest.TestCase):
    def test_long_document_tiles_overlap_and_cores_cover_the_page_once(self) -> None:
        tiles = _primary_tiles(
            (100, 300),
            TextRefinementSettings(
                primary_tile_max_aspect=1.25,
                primary_tile_overlap_ratio=0.15,
                primary_tile_max_count=4,
            ),
        )

        self.assertEqual(len(tiles), 3)
        self.assertEqual([(tile.core_start, tile.core_end) for tile in tiles], [(0, 102), (102, 198), (198, 300)])
        self.assertEqual(tiles[0].region.bottom - tiles[1].region.top, 15)
        self.assertEqual(tiles[1].region.bottom - tiles[2].region.top, 15)
        self.assertTrue(all(tile.axis == "y" for tile in tiles))
        self.assertTrue(all(tile.region.height / tile.region.width <= 1.25 for tile in tiles))

    def test_tiled_primary_maps_each_core_back_to_the_full_document(self) -> None:
        class Client:
            def __init__(self):
                self.calls = 0
                self.image_sizes = []

            def recognize(self, image_bytes, settings):
                import io
                from PIL import Image

                self.calls += 1
                with Image.open(io.BytesIO(image_bytes)) as image:
                    self.image_sizes.append(image.size)
                return {
                    "ok": True,
                    "ocr": [
                        {
                            "text": "tile-%d" % self.calls,
                            "box": [40, 40, 60, 60],
                            "polygon": [[40, 40], [60, 40], [60, 60], [40, 60]],
                            "score": 0.95,
                        }
                    ],
                }

        import io
        from PIL import Image

        output = io.BytesIO()
        Image.new("RGB", (100, 300), "white").save(output, format="JPEG")
        client = Client()
        extraction = FullTextExtractor(
            client,
            OcrSettings("http://127.0.0.1:5002/ocr", 3.0),
            TextRefinementSettings(
                primary_tile_max_aspect=1.25,
                primary_tile_overlap_ratio=0.15,
                primary_tile_max_count=4,
            ),
        ).extract_refined(output.getvalue())

        self.assertEqual(client.calls, 3)
        self.assertEqual(client.image_sizes, [(110, 110), (110, 110), (110, 110)])
        self.assertTrue(extraction.accepted)
        self.assertEqual(extraction.timings["primary_tile_count"], 3.0)
        blocks = extraction.document.to_dict()["blocks"]
        self.assertEqual([block["text"] for block in blocks], ["tile-1", "tile-2", "tile-3"])
        self.assertEqual([block["box"][1] for block in blocks], [40, 135, 230])
        self.assertEqual(
            [block["recognition_source"] for block in blocks],
            ["primary_tile_1", "primary_tile_2", "primary_tile_3"],
        )

    def test_preserves_every_block_and_builds_reading_order_text(self) -> None:
        document = build_captured_text_document(
            [
                span(3, 2, "检查项目", (20, 200, 100, 230), 0.91),
                span(1, 1, "姓名", (20, 100, 60, 130), 0.99),
                span(2, 1, "张三", (100, 100, 150, 130), 0.97),
                span(4, 2, "腹部超声", (120, 200, 220, 230), 0.93),
            ],
            (1000, 1000),
        )

        self.assertEqual(document.full_text, "姓名 张三\n检查项目 腹部超声")
        self.assertEqual([item.text for item in document.spans], ["姓名", "张三", "检查项目", "腹部超声"])
        self.assertEqual(document.lines[0].span_ids, (1, 2))
        self.assertEqual(document.public_status()["item_count"], 4)
        self.assertEqual(document.to_dict()["blocks"][3]["text"], "腹部超声")
        self.assertEqual(document.to_dict()["schema_version"], 2)

    def test_rejects_invalid_image_size(self) -> None:
        with self.assertRaisesRegex(ValueError, "image size"):
            build_captured_text_document([], (0, 100))

    def test_full_text_extractor_is_independent_from_identifier_rules(self) -> None:
        class Client:
            def recognize(self, image_bytes, settings):
                self.image_bytes = image_bytes
                return {
                    "ok": True,
                    "ocr": [
                        {"text": "姓名", "box": [10, 10, 80, 40], "score": 0.98},
                        {"text": "测试患者", "box": [100, 10, 220, 40], "score": 0.97},
                    ],
                }

        import io
        from PIL import Image

        output = io.BytesIO()
        Image.new("RGB", (400, 200), "white").save(output, format="JPEG")
        client = Client()
        extraction = FullTextExtractor(
            client,
            OcrSettings("http://127.0.0.1:5002/ocr", 3.0),
        ).extract(output.getvalue())

        self.assertTrue(extraction.accepted)
        self.assertEqual(extraction.document.full_text, "姓名 测试患者")
        self.assertNotIn("测试患者", str(extraction.public_status()))
        self.assertEqual(extraction.public_status()["item_count"], 2)

    def test_clean_refined_extraction_calls_ocr_once_and_preserves_polygon(self) -> None:
        class Client:
            def __init__(self):
                self.calls = 0

            def recognize(self, image_bytes, settings):
                self.calls += 1
                return {
                    "ok": True,
                    "ocr": [
                        {
                            "text": "检查项目",
                            "box": [100, 100, 220, 140],
                            "polygon": [[100, 100], [220, 100], [220, 140], [100, 140]],
                            "score": 0.96,
                        }
                    ],
                }

        import io
        from PIL import Image

        output = io.BytesIO()
        Image.new("RGB", (400, 300), "white").save(output, format="JPEG")
        client = Client()
        extraction = FullTextExtractor(
            client,
            OcrSettings("http://127.0.0.1:5002/ocr", 3.0),
        ).extract_refined(output.getvalue(), output.getvalue())

        self.assertEqual(client.calls, 1)
        self.assertTrue(extraction.accepted)
        block = extraction.document.to_dict()["blocks"][0]
        self.assertEqual(block["polygon"], [[100, 100], [220, 100], [220, 140], [100, 140]])
        self.assertEqual(block["normalized_polygon"][0], [250, 333])
        self.assertEqual(block["recognition_source"], "primary")

    def test_low_confidence_region_is_refined_and_close_conflict_is_retained(self) -> None:
        class Client:
            def __init__(self):
                self.calls = 0

            def recognize(self, image_bytes, settings):
                self.calls += 1
                if self.calls == 1:
                    return {
                        "ok": True,
                        "ocr": [{"text": "患者编号", "box": [100, 100, 200, 140], "score": 0.60}],
                    }
                return {
                    "ok": True,
                    "ocr": [{"text": "患者编召", "box": [320, 320, 720, 480], "score": 0.64}],
                }

        import io
        from PIL import Image

        output = io.BytesIO()
        Image.new("RGB", (400, 300), "white").save(output, format="JPEG")
        client = Client()
        extraction = FullTextExtractor(
            client,
            OcrSettings("http://127.0.0.1:5002/ocr", 3.0),
            TextRefinementSettings(max_regions=3),
        ).extract_refined(output.getvalue(), output.getvalue())

        self.assertEqual(client.calls, 2)
        self.assertEqual(extraction.status, "review_required")
        self.assertEqual(extraction.refinement_regions, 1)
        self.assertEqual(extraction.conflict_count, 1)
        block = extraction.document.to_dict()["blocks"][0]
        self.assertEqual(block["text"], "患者编号")
        self.assertEqual(block["alternatives"][0]["text"], "患者编召")

    def test_zero_refinement_regions_keeps_low_confidence_primary_without_retry(self) -> None:
        class Client:
            def __init__(self):
                self.calls = 0

            def recognize(self, image_bytes, settings):
                self.calls += 1
                return {
                    "ok": True,
                    "ocr": [
                        {"text": "申请号", "box": [100, 100, 220, 140], "score": 0.60}
                    ],
                }

        import io
        from PIL import Image

        output = io.BytesIO()
        Image.new("RGB", (400, 300), "white").save(output, format="JPEG")
        client = Client()
        extraction = FullTextExtractor(
            client,
            OcrSettings("http://127.0.0.1:5002/ocr", 3.0),
            TextRefinementSettings(max_regions=0),
        ).extract_refined(output.getvalue(), output.getvalue())

        self.assertEqual(client.calls, 1)
        self.assertEqual(extraction.refinement_regions, 0)
        self.assertEqual(extraction.status, "review_required")
        self.assertIn("low_confidence_blocks", extraction.reasons)
        self.assertNotIn("ocr_conflict", extraction.reasons)

    def test_empty_primary_uses_one_secondary_full_page_retry(self) -> None:
        class Client:
            def __init__(self):
                self.calls = 0

            def recognize(self, image_bytes, settings):
                self.calls += 1
                if self.calls == 1:
                    return {"ok": True, "ocr": []}
                return {
                    "ok": True,
                    "ocr": [{"text": "门诊申请单", "box": [100, 80, 260, 120], "score": 0.95}],
                }

        import io
        from PIL import Image

        output = io.BytesIO()
        Image.new("RGB", (400, 300), "white").save(output, format="JPEG")
        client = Client()
        extraction = FullTextExtractor(
            client,
            OcrSettings("http://127.0.0.1:5002/ocr", 3.0),
        ).extract_refined(output.getvalue(), output.getvalue())

        self.assertEqual(client.calls, 2)
        self.assertTrue(extraction.available)
        self.assertEqual(extraction.document.spans[0].recognition_source, "secondary_full")

    def test_refinement_joining_multiple_primary_blocks_is_not_a_false_conflict(self) -> None:
        primary = [
            {"text": "姓名：", "box": [10, 10, 80, 40], "score": 0.68},
            {"text": "张三", "box": [90, 10, 150, 40], "score": 0.69},
        ]
        refinement = [
            {"text": "姓名：张三", "box": [8, 8, 152, 42], "score": 0.70},
        ]

        merged, conflicts = _merge_items(primary, refinement, 0.08)

        self.assertEqual(conflicts, 0)
        self.assertEqual([item["text"] for item in merged], ["姓名：", "张三"])
        self.assertTrue(all(not item.get("alternatives") for item in merged))

    def test_clear_score_winner_does_not_keep_a_low_score_alternative(self) -> None:
        primary = [{"text": "申请号", "box": [10, 10, 100, 40], "score": 0.55}]
        refinement = [{"text": "申请单号", "box": [10, 10, 100, 40], "score": 0.90}]

        merged, conflicts = _merge_items(primary, refinement, 0.08)

        self.assertEqual(conflicts, 0)
        self.assertEqual(merged[0]["text"], "申请单号")
        self.assertEqual(merged[0]["alternatives"], [])


if __name__ == "__main__":
    unittest.main()
