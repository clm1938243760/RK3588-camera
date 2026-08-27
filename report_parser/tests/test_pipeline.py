from __future__ import annotations

import io
import json
import unittest

from PIL import Image, ImageDraw

from rk3588_report_parser.models import FIELD_NAMES
from rk3588_report_parser.pipeline import ReportParser
from rk3588_report_parser.settings import LlmSettings, OcrSettings, ParserSettings, QualitySettings, ValidationSettings


class FakeOcrClient:
    def __init__(self, score: float = 0.98) -> None:
        self.score = score

    def recognize(self, image_bytes, settings):
        rows = [
            (80, "姓名", "张三"),
            (150, "患者ID", "P2605260007"),
            (220, "性别", "男"),
            (290, "年龄", "45岁"),
            (360, "出生日期", "1981-02-03"),
            (430, "报告号", "R202608100001"),
            (500, "报告日期", "2026-08-10"),
            (570, "检查项目", "腹部超声"),
        ]
        items = []
        for y, label, value in rows:
            items.append({"text": label, "score": self.score, "box": [80, y, 180, y + 35]})
            items.append({"text": value, "score": self.score, "box": [240, y, 520, y + 35]})
        return {"ok": True, "ocr": items}


class FailedOcrClient:
    def recognize(self, image_bytes, settings):
        return {"ok": False, "ocr": []}


class FakeLinker:
    def __init__(self, patient_id: int = 4) -> None:
        self.patient_id = patient_id

    def link(self, system_prompt, user_prompt, settings):
        payload = {field: {"span_ids": []} for field in FIELD_NAMES}
        payload.update(
            {
                "patient_name": {"span_ids": [2]},
                "patient_id": {"span_ids": [self.patient_id]},
                "sex": {"span_ids": [6]},
                "age": {"span_ids": [8]},
                "birthday": {"span_ids": [10]},
                "report_no": {"span_ids": [12]},
                "report_date": {"span_ids": [14]},
                "exam_item": {"span_ids": [16]},
            }
        )
        return json.dumps(payload, ensure_ascii=False)


class InvalidLinker:
    def link(self, system_prompt, user_prompt, settings):
        return "not JSON"


class UnanchoredIdentifierOcrClient:
    def recognize(self, image_bytes, settings):
        return {
            "ok": True,
            "ocr": [
                {"text": "Name", "score": 0.98, "box": [80, 80, 180, 115]},
                {"text": "Alice", "score": 0.98, "box": [240, 80, 420, 115]},
                {"text": "P2605260007", "score": 0.98, "box": [240, 150, 520, 185]},
            ],
        }


class UnanchoredIdentifierLinker:
    def link(self, system_prompt, user_prompt, settings):
        return json.dumps(
            {
                "patient_name": [2],
                "patient_id": [3],
                "sex": [],
                "age": [],
                "birthday": [],
                "his_exam_no": [],
                "report_no": [],
                "report_date": [],
                "exam_item": [],
            }
        )


def settings() -> ParserSettings:
    return ParserSettings(
        ocr=OcrSettings(endpoint="http://127.0.0.1:5002/ocr", timeout_seconds=5),
        llm=LlmSettings(
            endpoint="http://127.0.0.1:8010/v1/chat/completions",
            model="fake",
            timeout_seconds=5,
            max_tokens=256,
        ),
        quality=QualitySettings(
            min_longest_side=1600,
            min_contrast=6,
            min_laplacian_energy=25,
            min_ocr_items=3,
            min_ocr_score=0.65,
        ),
        validation=ValidationSettings(max_age_delta_years=2, require_patient_name=True, require_identifier=True),
    )


def report_image() -> bytes:
    image = Image.new("RGB", (1800, 2000), "white")
    draw = ImageDraw.Draw(image)
    for y in range(100, 1850, 40):
        draw.rectangle((100, y, 1650, y + 9), fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


class PipelineTests(unittest.TestCase):
    def test_accepts_evidence_backed_fields(self) -> None:
        outcome = ReportParser(settings(), FakeOcrClient(), FakeLinker()).parse_bytes(report_image())
        result = outcome.result

        self.assertEqual(result.status, "accepted")
        self.assertEqual(result.fields["patient_name"].value, "张三")
        self.assertEqual(result.fields["patient_id"].value, "P2605260007")
        self.assertEqual(result.fields["exam_item"].value, "腹部超声")
        for field in FIELD_NAMES:
            evidence = result.fields[field]
            if evidence.value:
                self.assertTrue(evidence.source_span_ids)

    def test_unknown_model_span_rejects_without_free_text(self) -> None:
        outcome = ReportParser(settings(), FakeOcrClient(), FakeLinker(patient_id=99)).parse_bytes(report_image())
        result = outcome.result

        self.assertEqual(result.status, "rejected")
        self.assertIn("patient_id:unknown_span_id:99", result.rejection_reasons)
        self.assertEqual(result.fields["patient_id"].value, "")

    def test_low_ocr_confidence_rejects_before_model_result(self) -> None:
        outcome = ReportParser(settings(), FakeOcrClient(score=0.3), FakeLinker()).parse_bytes(report_image())
        self.assertEqual(outcome.result.status, "rejected")
        self.assertIn("low_ocr_confidence", outcome.result.rejection_reasons)
        self.assertEqual(outcome.result.ocr_summary["item_count"], 16)

    def test_ocr_failure_response_rejects_before_model_result(self) -> None:
        outcome = ReportParser(settings(), FailedOcrClient(), FakeLinker()).parse_bytes(report_image())
        self.assertEqual(outcome.result.status, "rejected")
        self.assertIn("ocr_service_failed", outcome.result.rejection_reasons)

    def test_hybrid_mode_uses_unambiguous_label_geometry_after_invalid_model_json(self) -> None:
        outcome = ReportParser(
            settings(),
            FakeOcrClient(),
            InvalidLinker(),
            association_mode="hybrid",
        ).parse_bytes(report_image())

        self.assertEqual(outcome.result.status, "accepted")
        self.assertFalse(outcome.result.association["model_response_valid"])
        self.assertIn("patient_name", outcome.result.association["label_geometry_fields"])
        self.assertEqual(outcome.result.fields["patient_id"].value, "P2605260007")

    def test_hybrid_mode_discards_unanchored_model_identifier(self) -> None:
        outcome = ReportParser(
            settings(),
            UnanchoredIdentifierOcrClient(),
            UnanchoredIdentifierLinker(),
            association_mode="hybrid",
        ).parse_bytes(report_image())

        self.assertEqual(outcome.result.status, "rejected")
        self.assertEqual(outcome.result.fields["patient_name"].value, "Alice")
        self.assertEqual(outcome.result.fields["patient_id"].value, "")
        self.assertIn("patient_id", outcome.result.association["discarded_unanchored_model_fields"])
        self.assertIn("missing_patient_identifier", outcome.result.rejection_reasons)


if __name__ == "__main__":
    unittest.main()
