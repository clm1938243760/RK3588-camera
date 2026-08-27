from __future__ import annotations

import unittest
from pathlib import Path

from rk3588_report_parser.uie_extraction import (
    blocks_from_payload,
    build_layout_evidence_document,
    build_evidence_document,
    extract_uie_fields,
    load_uie_schema,
    run_uie_extraction,
    run_uie_x_extraction,
)


def blocks():
    return [
        {"id": 1, "line_id": 1, "text": "姓名", "score": 0.99, "box": [10, 10, 60, 30], "normalized_box": [10, 10, 60, 30]},
        {"id": 2, "line_id": 1, "text": "张三", "score": 0.98, "box": [80, 10, 130, 30], "normalized_box": [80, 10, 130, 30]},
        {"id": 3, "line_id": 2, "text": "患者ID", "score": 0.97, "box": [10, 50, 80, 70], "normalized_box": [10, 50, 80, 70]},
        {"id": 4, "line_id": 2, "text": "60019825336", "score": 0.96, "box": [100, 50, 250, 70], "normalized_box": [100, 50, 250, 70]},
    ]


class UieEvidenceTests(unittest.TestCase):
    def test_default_schema_loads_without_an_active_file(self) -> None:
        schema = load_uie_schema(None)
        self.assertEqual(schema[0]["field_key"], "patient_name")
        self.assertEqual(schema[0]["minimum_probability"], 0.5)
        exam_number = next(item for item in schema if item["field_key"] == "his_exam_no")
        self.assertIn("处方/申请号", exam_number["prompt_aliases"])

    def test_prompt_alias_can_extract_and_deduplicate_the_same_ocr_value(self) -> None:
        document = build_evidence_document([
            {
                "id": 1,
                "line_id": 1,
                "text": "处方/申请号：02D2026080500192",
                "score": 0.99,
                "box": [10, 10, 300, 30],
            }
        ])
        value = "02D2026080500192"
        start = document.text.index(value)
        prediction = {"text": value, "start": start, "end": start + len(value), "probability": 0.91}
        result = extract_uie_fields(
            document,
            [{
                "field_key": "his_exam_no",
                "prompt": "检查号或申请单号",
                "prompt_aliases": ["申请单号", "处方/申请号"],
                "minimum_probability": 0.5,
            }],
            {"申请单号": [prediction], "处方/申请号": [dict(prediction)]},
            "fake-uie",
        )
        field = result["fields"]["his_exam_no"]
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(field["value"], value)
        self.assertEqual(field["matched_prompt"], "申请单号")
        self.assertEqual(field["alternatives"], [])

    def test_top_level_ocr_span_array_is_supported(self) -> None:
        result = blocks_from_payload(blocks())
        self.assertEqual([item["id"] for item in result], [1, 2, 3, 4])

    def test_uie_x_uses_image_layout_and_maps_offsets(self) -> None:
        seen = []
        schema = [{"field_key": "patient_id", "prompt": "patient id", "minimum_probability": 0.5}]

        def predict(image_path, layout):
            seen.append((str(image_path), layout))
            document, _ = build_layout_evidence_document(blocks())
            start = document.text.index("60019825336")
            return {"patient id": [{
                "text": "60019825336",
                "start": start,
                "end": start + 11,
                "probability": 0.98,
            }]}

        result = run_uie_x_extraction(
            blocks(), schema, Path("report.jpg"), predict
        )
        self.assertEqual(result["fields"]["patient_id"]["source_span_ids"], [4])
        self.assertEqual(seen[0][0], "report.jpg")
        self.assertEqual(seen[0][1][0], [[10, 10, 60, 30], "姓名"])

    def test_low_probability_optional_prediction_does_not_force_review(self) -> None:
        document = build_evidence_document(blocks())
        name_start = document.text.index("张三")
        id_start = document.text.index("60019825336")
        result = extract_uie_fields(
            document,
            [
                {"field_key": "patient_name", "prompt": "name", "minimum_probability": 0.5},
                {"field_key": "patient_id", "prompt": "id", "minimum_probability": 0.5},
            ],
            {
                "name": [{"text": "张三", "start": name_start, "end": name_start + 2, "probability": 0.9}],
                "id": [{
                    "text": "60019825336", "start": id_start, "end": id_start + 11, "probability": 0.4,
                }],
            },
            "fake-uie",
        )
        self.assertEqual(result["status"], "accepted")

    def test_patient_response_normalizes_age_birthday_and_compound_surname(self) -> None:
        document = build_evidence_document([
            {"id": 1, "line_id": 1, "text": "欧阳明", "score": 0.99, "box": [0, 0, 20, 10]},
            {"id": 2, "line_id": 2, "text": "63岁", "score": 0.99, "box": [0, 20, 20, 30]},
            {"id": 3, "line_id": 3, "text": "1988年04月14日", "score": 0.99, "box": [0, 40, 50, 50]},
        ])
        response = {}
        schema = []
        for field_key, prompt, value in (
            ("patient_name", "name", "欧阳明"),
            ("age", "age", "63岁"),
            ("birthday", "birthday", "1988年04月14日"),
        ):
            start = document.text.index(value)
            schema.append({"field_key": field_key, "prompt": prompt, "minimum_probability": 0.5})
            response[prompt] = [{
                "text": value, "start": start, "end": start + len(value), "probability": 0.99,
            }]
        result = extract_uie_fields(document, schema, response, "fake-uie")
        patient = result["patient_response"]["data"][0]
        self.assertEqual(patient["age"], "63")
        self.assertEqual(patient["birthday"], "1988-04-14")
        self.assertEqual((patient["xing"], patient["ming"]), ("欧阳", "明"))
        self.assertEqual((patient["nian"], patient["yue"], patient["ri"]), ("1988", "04", "14"))

    def test_document_offsets_map_back_to_ocr_spans(self) -> None:
        document = build_evidence_document(blocks())
        self.assertEqual(document.text, "姓名 张三\n患者ID 60019825336")
        start = document.text.index("张三")
        result = extract_uie_fields(
            document,
            [{"field_key": "patient_name", "prompt": "患者姓名", "minimum_probability": 0.5}],
            {"患者姓名": [{"text": "张三", "start": start, "end": start + 2, "probability": 0.99}]},
            "fake-uie",
        )
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["fields"]["patient_name"]["source_span_ids"], [2])
        self.assertEqual(result["patient_response"]["data"][0]["xing"], "张")
        self.assertEqual(result["patient_response"]["data"][0]["ming"], "三")

    def test_non_source_text_is_rejected(self) -> None:
        document = build_evidence_document(blocks())
        start = document.text.index("张三")
        result = extract_uie_fields(
            document,
            [{"field_key": "patient_name", "prompt": "患者姓名", "minimum_probability": 0.5}],
            {"患者姓名": [{"text": "李四", "start": start, "end": start + 2, "probability": 0.99}]},
            "fake-uie",
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["rejected_predictions"][0]["reason"], "text_not_from_ocr")

    def test_low_probability_and_required_field_need_review(self) -> None:
        document = build_evidence_document(blocks())
        start = document.text.index("张三")
        result = extract_uie_fields(
            document,
            [{"field_key": "patient_name", "prompt": "患者姓名", "required": True, "minimum_probability": 0.8}],
            {"患者姓名": [{"text": "张三", "start": start, "end": start + 2, "probability": 0.7}]},
            "fake-uie",
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["missing_fields"], ["patient_name"])

    def test_predictor_receives_only_assembled_ocr_text(self) -> None:
        seen = []
        schema = [{"field_key": "patient_id", "prompt": "患者ID", "minimum_probability": 0.5}]

        def predict(text):
            seen.append(text)
            start = text.index("60019825336")
            return {"患者ID": [{"text": "60019825336", "start": start, "end": start + 11, "probability": 0.95}]}

        result = run_uie_extraction(blocks(), schema, "fake-uie", predict)
        self.assertEqual(seen, ["姓名 张三\n患者ID 60019825336"])
        self.assertEqual(result["fields"]["patient_id"]["source_span_ids"], [4])

    def test_field_validators_block_cross_type_predictions_and_use_ocr_fallbacks(self) -> None:
        document = build_evidence_document([
            {"id": 1, "line_id": 1, "text": "年龄：29岁", "score": 0.91, "box": [0, 0, 80, 20]},
            {"id": 2, "line_id": 2, "text": "世制：女", "score": 0.68, "box": [0, 25, 80, 45]},
            {"id": 3, "line_id": 3, "text": "项目名称", "score": 0.93, "box": [0, 50, 80, 70]},
            {"id": 4, "line_id": 4, "text": "62353胸部正位", "score": 0.86, "box": [0, 75, 150, 95]},
        ])
        wrong_sex_start = document.text.index("29岁")
        wrong_item_start = document.text.index("62353")
        result = extract_uie_fields(
            document,
            [
                {"field_key": "sex", "prompt": "患者性别", "minimum_probability": 0.5},
                {"field_key": "exam_item", "prompt": "检查项目", "prompt_aliases": ["项目名称"], "minimum_probability": 0.5},
            ],
            {
                "患者性别": [{"text": "29岁", "start": wrong_sex_start, "end": wrong_sex_start + 3, "probability": 0.91}],
                "检查项目": [{"text": "62353", "start": wrong_item_start, "end": wrong_item_start + 5, "probability": 0.88}],
            },
            "fake-uie",
        )

        self.assertEqual(result["fields"]["sex"]["value"], "女")
        self.assertEqual(result["fields"]["sex"]["resolution_method"], "typed_unique_fallback")
        self.assertEqual(result["fields"]["exam_item"]["value"], "胸部正位")
        self.assertEqual(result["fields"]["exam_item"]["resolution_method"], "label_neighbor_fallback")
        self.assertEqual(result["status"], "review_required")
        self.assertEqual(result["review_fields"], ["sex"])
        self.assertEqual(
            {item["reason"] for item in result["rejected_predictions"]},
            {"invalid_sex_value", "invalid_exam_item"},
        )

    def test_identifier_prediction_is_tightened_to_one_ocr_backed_token(self) -> None:
        document = build_evidence_document([
            {"id": 1, "line_id": 1, "text": "处方/申请号：", "score": 0.95, "box": [0, 0, 100, 20]},
            {"id": 2, "line_id": 2, "text": "01D2026073013779", "score": 0.97, "box": [0, 25, 150, 45]},
            {"id": 3, "line_id": 3, "text": "号输码", "score": 0.75, "box": [0, 50, 80, 70]},
        ])
        raw_value = "01D2026073013779\n号"
        start = document.text.index("01D2026073013779")
        result = extract_uie_fields(
            document,
            [{"field_key": "his_exam_no", "prompt": "申请号", "minimum_probability": 0.5}],
            {"申请号": [{"text": raw_value, "start": start, "end": start + len(raw_value), "probability": 0.9}]},
            "fake-uie",
        )

        field = result["fields"]["his_exam_no"]
        self.assertEqual(field["value"], "01D2026073013779")
        self.assertEqual(field["raw_value"], raw_value)
        self.assertEqual(field["source_span_ids"], [2])
        self.assertEqual(field["resolution_method"], "uie_typed_refinement")
        self.assertEqual(result["status"], "accepted")

    def test_invalid_typed_prediction_is_not_written_to_patient_json(self) -> None:
        document = build_evidence_document([
            {"id": 1, "line_id": 1, "text": "年龄：29岁", "score": 0.9, "box": [0, 0, 80, 20]},
        ])
        start = document.text.index("29岁")
        result = extract_uie_fields(
            document,
            [{"field_key": "sex", "prompt": "患者性别", "minimum_probability": 0.5}],
            {"患者性别": [{"text": "29岁", "start": start, "end": start + 3, "probability": 0.99}]},
            "fake-uie",
        )

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["patient_response"]["data"], [])
        self.assertNotIn("sex", result["fields"])


if __name__ == "__main__":
    unittest.main()
