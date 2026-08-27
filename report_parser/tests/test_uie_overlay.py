from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


def _overlay_module():
    path = Path(__file__).resolve().parents[1] / "board_camera_ocr_overlay.py"
    spec = importlib.util.spec_from_file_location("camera_ocr_overlay_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load camera overlay module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UieOverlayStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.overlay = _overlay_module()

    def test_exposes_only_current_capture_and_standard_patient_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "uie-patient.json"
            result_path.write_text(json.dumps({
                "status": "review_required",
                "capture_id": "capture-1",
                "fields": {
                    "patient_name": {
                        "value": "张三",
                        "probability": 0.98,
                        "source_span_ids": [4],
                        "matched_prompt": "患者姓名",
                    },
                    "unexpected": {
                        "value": "discard",
                        "probability": 1.0,
                        "source_span_ids": [5],
                    },
                },
                "review_fields": ["patient_name", "unexpected"],
                "patient_response": {
                    "code": "SUCCESS",
                    "data": [{"patient_name": "张三", "other": "discard"}],
                    "msg": "成功",
                    "success": True,
                },
                "timings": {"uie_ms": 123.456, "unexpected": 1},
            }, ensure_ascii=False), encoding="utf-8")
            store = self.overlay.UiePatientResultStore(result_path)

            current = store.snapshot({"active": True, "capture_id": "capture-1"})
            stale = store.snapshot({"active": True, "capture_id": "capture-2"})
            removed = store.snapshot({"active": False, "capture_id": "capture-1"})

            self.assertTrue(current["available"])
            self.assertEqual(list(current["fields"]), ["patient_name"])
            self.assertEqual(current["review_fields"], ["patient_name"])
            self.assertEqual(current["patient_json"]["data"][0]["patient_name"], "张三")
            self.assertNotIn("other", current["patient_json"]["data"][0])
            self.assertEqual(current["timings"], {"uie_ms": 123.46})
            self.assertFalse(stale["available"])
            self.assertFalse(removed["available"])

    def test_schema_payload_allows_standard_fields_and_normalizes_aliases(self) -> None:
        payload = self.overlay._normalize_uie_schema_payload({
            "fields": [
                {
                    "field_key": "patient_name",
                    "prompt": "姓名",
                    "prompt_aliases": ["患者姓名", "姓名", ""],
                    "required": True,
                    "minimum_probability": 0.75,
                },
                {
                    "field_key": "patient_id",
                    "prompt": "卡号",
                    "prompt_aliases": [],
                    "required": False,
                    "minimum_probability": "0.5",
                },
            ]
        })

        self.assertEqual(payload["fields"][0]["prompt_aliases"], ["患者姓名"])
        self.assertEqual(payload["fields"][0]["minimum_probability"], 0.75)
        self.assertEqual(payload["fields"][1]["minimum_probability"], 0.5)

    def test_schema_payload_rejects_duplicate_prompt_or_unsupported_field(self) -> None:
        with self.assertRaisesRegex(ValueError, "unique"):
            self.overlay._normalize_uie_schema_payload({
                "fields": [
                    {"field_key": "patient_name", "prompt": "编号"},
                    {"field_key": "patient_id", "prompt": "编号"},
                ]
            })
        with self.assertRaisesRegex(ValueError, "field key"):
            self.overlay._normalize_uie_schema_payload({
                "fields": [{"field_key": "unsupported", "prompt": "编号"}]
            })


class FixedFieldOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.overlay = _overlay_module()

    def test_fixed_rule_payload_uses_one_label_and_position(self) -> None:
        payload = self.overlay._normalize_fixed_rule_payload({
            "fields": [{
                "field_key": "patient_id",
                "label": "卡号",
                "position": "right_then_below",
                "char_type": "digits",
                "fixed_length": 11,
                "min_ocr_score": 0.75,
                "max_distance": 180,
                "required": True,
            }]
        })

        self.assertEqual(payload["fields"][0]["label"], "卡号")
        self.assertEqual(payload["fields"][0]["fixed_length"], 11)

    def test_fixed_rule_payload_rejects_duplicate_label(self) -> None:
        with self.assertRaisesRegex(ValueError, "unique fixed label"):
            self.overlay._normalize_fixed_rule_payload({
                "fields": [
                    {"field_key": "patient_id", "label": "编号"},
                    {"field_key": "his_exam_no", "label": "编号"},
                ]
            })

    def test_fixed_field_result_proxy_exposes_current_capture(self) -> None:
        class FakeClient:
            def request(self, method="GET", payload=None, query=None):
                return {
                    "available": True,
                    "status": "accepted",
                    "capture_id": query["capture_id"],
                    "fields": {
                        "patient_id": {
                            "value": "60019825336",
                            "probability": 0.98,
                            "source_span_ids": [1, 2],
                            "fixed_label": "卡号",
                            "relation": "same_line_right",
                        }
                    },
                    "review_fields": [],
                    "patient_json": {
                        "code": "SUCCESS", "data": [{"patient_id": "60019825336"}],
                        "msg": "成功", "success": True,
                    },
                }

        proxy = self.overlay.FieldPatientResultProxy(
            "http://127.0.0.1:8443/internal/v1/field-result"
        )
        proxy.client = FakeClient()

        result = proxy.snapshot({"active": True, "capture_id": "capture-1"})

        self.assertTrue(result["available"])
        self.assertEqual(result["engine"], "fixed_label_rules")
        self.assertEqual(result["fields"]["patient_id"]["fixed_label"], "卡号")
        self.assertEqual(result["fields"]["patient_id"]["source_span_ids"], [1, 2])

    def test_page_has_fixed_label_controls_without_uie_branding(self) -> None:
        page = self.overlay.PAGE.decode("utf-8")
        self.assertIn("固定标签字段配置", page)
        self.assertIn("右侧优先，再下方", page)
        self.assertIn("/api/field-rules", page)
        self.assertNotIn("UIE", page)

    def test_default_fixed_rules_asset_is_valid(self) -> None:
        path = Path(__file__).resolve().parents[1] / "runtime" / "active_fixed_field_rules.json"
        payload = json.loads(path.read_text(encoding="utf-8"))

        normalized = self.overlay._normalize_fixed_rule_payload(payload)

        self.assertEqual(len(normalized["fields"]), 9)
        self.assertEqual(normalized["fields"][1]["label"], "卡号")


if __name__ == "__main__":
    unittest.main()
