from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from rk3588_report_parser.settings import OcrSettings
from rk3588_report_parser.uie_extraction import load_uie_schema
from rk3588_report_parser.uie_patient_service import UiePatientService


class FakeEngine:
    def __init__(self) -> None:
        self.prompts = []
        self.calls = 0

    def predict(self, text):
        self.calls += 1
        name_start = text.index("张三")
        id_start = text.index("60019825336")
        return {
            "患者姓名": [{"text": "张三", "start": name_start, "end": name_start + 2, "probability": 0.98}],
            "患者ID": [{"text": "60019825336", "start": id_start, "end": id_start + 11, "probability": 0.97}],
        }

    def set_prompts(self, prompts):
        self.prompts = list(prompts)


class FakeOcr:
    def __init__(self) -> None:
        self.calls = 0

    def recognize(self, image_bytes, settings):
        self.calls += 1
        return {
            "ok": True,
            "ocr": [
                {"text": "姓名 张三", "score": 0.99, "box": [10, 10, 90, 30]},
                {"text": "患者ID 60019825336", "score": 0.98, "box": [10, 40, 180, 60]},
            ],
        }


class AlternativeEngine(FakeEngine):
    def predict(self, text):
        self.calls += 1
        name_start = text.index("张三")
        first_start = text.index("P10001")
        second_start = text.index("P20002")
        return {
            "患者姓名": [{"text": "张三", "start": name_start, "end": name_start + 2, "probability": 0.99}],
            "患者ID": [
                {"text": "P10001", "start": first_start, "end": first_start + 6, "probability": 0.99},
                {"text": "P20002", "start": second_start, "end": second_start + 6, "probability": 0.98},
            ],
        }


class WrongSexEngine(FakeEngine):
    def predict(self, text):
        self.calls += 1
        start = text.index("29岁")
        return {
            "患者性别": [{
                "text": "29岁",
                "start": start,
                "end": start + 3,
                "probability": 0.98,
            }],
        }


def schema():
    return [
        {"field_key": "patient_name", "prompt": "患者姓名", "minimum_probability": 0.5},
        {"field_key": "patient_id", "prompt": "患者ID", "minimum_probability": 0.5},
    ]


def image_bytes():
    output = io.BytesIO()
    Image.new("RGB", (200, 100), "white").save(output, format="JPEG")
    return output.getvalue()


class UiePatientServiceTests(unittest.TestCase):
    def test_uploaded_image_runs_ocr_then_uie_and_returns_patient_shape(self) -> None:
        engine = FakeEngine()
        ocr = FakeOcr()
        service = UiePatientService(
            engine,
            schema(),
            "uie-base",
            OcrSettings("http://127.0.0.1:5002/ocr", 20),
            ocr_client=ocr,
        )

        result = service.parse_image(image_bytes())

        self.assertEqual(ocr.calls, 1)
        self.assertEqual(engine.calls, 1)
        self.assertEqual(result["patient_response"]["code"], "SUCCESS")
        self.assertEqual(result["patient_response"]["data"][0]["patient_name"], "张三")
        self.assertEqual(result["fields"]["patient_id"]["source_span_ids"], [2])
        self.assertEqual(result["document"]["image_size"], [200, 100])
        self.assertEqual(result["document"]["blocks"][1]["normalized_box"], [50, 400, 900, 600])

    def test_camera_capture_uses_existing_ocr_and_is_idempotent(self) -> None:
        engine = FakeEngine()
        ocr = FakeOcr()
        service = UiePatientService(
            engine,
            schema(),
            "uie-base",
            OcrSettings("http://127.0.0.1:5002/ocr", 20),
            ocr_client=ocr,
        )
        capture = {
            "status": "accepted",
            "capture_id": "capture-001",
            "source": {"frame_size": {"width": 200, "height": 100}},
            "document": {
                "schema_version": 2,
                "image_size": [200, 100],
                "blocks": FakeOcr().recognize(b"", None)["ocr"],
            },
        }

        first = service.parse_capture(capture)
        second = service.parse_capture(capture)

        self.assertEqual(first, second)
        self.assertEqual(engine.calls, 1)
        self.assertEqual(ocr.calls, 0)
        self.assertEqual(first["source"]["type"], "camera_ocr_schema_v2")

    def test_schema_update_is_validated_persisted_and_applied(self) -> None:
        engine = FakeEngine()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "active_uie_schema.json"
            service = UiePatientService(
                engine,
                schema(),
                "uie-base",
                OcrSettings("http://127.0.0.1:5002/ocr", 20),
                ocr_client=FakeOcr(),
                schema_path=path,
            )
            configured = service.update_schema({
                "fields": [
                    {"field_key": "patient_name", "prompt": "病人姓名", "minimum_probability": 0.7},
                    {"field_key": "patient_id", "prompt": "病人编号", "required": True, "minimum_probability": 0.8},
                ]
            })

            self.assertEqual(engine.prompts, ["病人姓名", "病人编号"])
            self.assertEqual(configured["fields"][1]["minimum_probability"], 0.8)
            self.assertEqual(load_uie_schema(path)[0]["prompt"], "病人姓名")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["schema_version"], 1)

    def test_unknown_patient_field_is_rejected(self) -> None:
        service = UiePatientService(
            FakeEngine(), schema(), "uie-base", OcrSettings("http://127.0.0.1:5002/ocr", 20),
            ocr_client=FakeOcr(),
        )
        with self.assertRaisesRegex(ValueError, "unsupported UIE patient field"):
            service.update_schema({"fields": [{"field_key": "unknown", "prompt": "未知"}]})

    def test_manual_candidate_selection_rebuilds_patient_json_from_ocr_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "latest.json"
            service = UiePatientService(
                AlternativeEngine(), schema(), "uie-base",
                OcrSettings("http://127.0.0.1:5002/ocr", 20),
                ocr_client=FakeOcr(), result_path=result_path,
            )
            capture = {
                "status": "accepted",
                "capture_id": "capture-review",
                "document": {
                    "schema_version": 2,
                    "image_size": [200, 100],
                    "blocks": [
                        {"id": 1, "line_id": 1, "text": "姓名 张三", "score": 0.99, "box": [0, 0, 80, 20]},
                        {"id": 2, "line_id": 2, "text": "患者ID P10001", "score": 0.99, "box": [0, 25, 100, 45]},
                        {"id": 3, "line_id": 3, "text": "患者ID P20002", "score": 0.99, "box": [0, 50, 100, 70]},
                    ],
                },
            }
            initial = service.parse_capture(capture)
            corrected = service.select_candidate("patient_id", 1)

            self.assertEqual(initial["status"], "review_required")
            self.assertEqual(corrected["status"], "accepted")
            self.assertEqual(corrected["patient_response"]["data"][0]["patient_id"], "P20002")
            self.assertEqual(corrected["fields"]["patient_id"]["source_span_ids"], [3])
            self.assertIn("patient_id", corrected["manual_corrections"])
            persisted = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["patient_response"]["data"][0]["patient_id"], "P20002")

    def test_manual_confirmation_accepts_a_typed_unique_ocr_fallback(self) -> None:
        service = UiePatientService(
            WrongSexEngine(),
            [{"field_key": "sex", "prompt": "患者性别", "minimum_probability": 0.5}],
            "uie-base",
            OcrSettings("http://127.0.0.1:5002/ocr", 20),
            ocr_client=FakeOcr(),
        )
        capture = {
            "status": "accepted",
            "capture_id": "capture-sex-review",
            "document": {
                "schema_version": 2,
                "image_size": [200, 100],
                "blocks": [
                    {"id": 1, "line_id": 1, "text": "年龄：29岁", "score": 0.92, "box": [0, 0, 80, 20]},
                    {"id": 2, "line_id": 2, "text": "世制：女", "score": 0.72, "box": [0, 25, 80, 45]},
                ],
            },
        }

        initial = service.parse_capture(capture)
        corrected = service.select_candidate("sex", 0)

        self.assertEqual(initial["status"], "review_required")
        self.assertEqual(initial["review_fields"], ["sex"])
        self.assertEqual(corrected["status"], "accepted")
        self.assertEqual(corrected["review_fields"], [])
        self.assertEqual(corrected["patient_response"]["data"][0]["sex"], "女")


if __name__ == "__main__":
    unittest.main()
