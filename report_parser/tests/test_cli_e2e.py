from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageDraw

from rk3588_report_parser.cli import main
from rk3588_report_parser.models import FIELD_NAMES


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _json(self, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if self.path == "/ocr":
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
            ocr = []
            for y, label, value in rows:
                ocr.append({"text": label, "score": 0.98, "box": [80, y, 180, y + 35]})
                ocr.append({"text": value, "score": 0.98, "box": [240, y, 520, y + 35]})
            self._json({"ok": True, "ocr": ocr})
            return
        if self.path == "/v1/chat/completions":
            request = json.loads(body.decode("utf-8"))
            user_payload = json.loads(request["messages"][-1]["content"])
            if "candidates" in user_payload and "allowed_types" in user_payload:
                matches = []
                type_by_label = {
                    "患者ID": "patient_id",
                    "报告号": "other_medical_id",
                }
                for candidate in user_payload["candidates"]:
                    identifier_type = type_by_label.get(candidate["label_text"])
                    if identifier_type:
                        matches.append({"candidate_id": candidate["candidate_id"], "type": identifier_type})
                self._json({"choices": [{"message": {"content": json.dumps({"classifications": matches})}}]})
                return
            if "classified_candidates" in user_payload:
                confirmed_ids = [item["candidate_id"] for item in user_payload["classified_candidates"]]
                self._json(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {"confirmed_candidate_ids": confirmed_ids}
                                    )
                                }
                            }
                        ]
                    }
                )
                return
            if "candidate_evidence_options" in user_payload:
                targets = {
                    "patient_name": ([1], [2]),
                    "patient_id": ([3], [4]),
                    "sex": ([5], [6]),
                    "age": ([7], [8]),
                    "birthday": ([9], [10]),
                    "report_no": ([11], [12]),
                    "report_date": ([13], [14]),
                    "exam_item": ([15], [16]),
                }
                label_ids, value_ids = targets.get(user_payload["field"], ([], []))
                option_id = next(
                    (
                        option["option_id"]
                        for option in user_payload["candidate_evidence_options"]
                        if option["label_span_ids"] == label_ids
                        and option["value_span_ids"] == value_ids
                    ),
                    0,
                )
                content = json.dumps({"option_id": option_id})
                self._json({"choices": [{"message": {"content": content}}]})
                return
            if user_payload.get("task", "").startswith("复核"):
                content = json.dumps({"confirmed": True})
                self._json({"choices": [{"message": {"content": content}}]})
                return
            links = {field: {"span_ids": []} for field in FIELD_NAMES}
            links.update(
                {
                    "patient_name": {"span_ids": [2]},
                    "patient_id": {"span_ids": [4]},
                    "sex": {"span_ids": [6]},
                    "age": {"span_ids": [8]},
                    "birthday": {"span_ids": [10]},
                    "report_no": {"span_ids": [12]},
                    "report_date": {"span_ids": [14]},
                    "exam_item": {"span_ids": [16]},
                }
            )
            self._json({"choices": [{"message": {"content": json.dumps(links, ensure_ascii=False)}}]})
            return
        if self.path == "/v1/span-choice":
            payload = json.loads(body.decode("utf-8"))
            prompt = json.loads(payload["messages"][-1]["content"])
            if prompt.get("stage") == "label":
                label_choices = {
                    "patient_name": 1,
                    "patient_id": 3,
                    "sex": 5,
                    "age": 7,
                    "birthday": 9,
                    "report_no": 11,
                    "report_date": 13,
                    "exam_item": 15,
                    "his_exam_no": 0,
                }
                self._json({"choice_id": label_choices[prompt["field"]]})
                return
            if prompt.get("stage") == "value":
                value_span_choices = {
                    "patient_name": [2],
                    "patient_id": [4],
                    "sex": [6],
                    "age": [8],
                    "birthday": [10],
                    "report_no": [12],
                    "report_date": [14],
                    "exam_item": [16],
                }
                expected = value_span_choices.get(prompt["field"], [])
                choice_id = next(
                    (
                        int(option["id"])
                        for option in prompt["selectable_options"]
                        if option.get("value_span_ids") == expected
                    ),
                    0,
                )
                self._json({"choice_id": choice_id})
                return
            choices = {
                "patient_name": 2,
                "patient_id": 4,
                "sex": 6,
                "age": 8,
                "birthday": 10,
                "report_no": 12,
                "report_date": 14,
                "exam_item": 16,
                "his_exam_no": 0,
            }
            self._json({"choice_id": choices[prompt["field"]]})
            return
        self.send_error(404)


def _report_image(path: Path) -> None:
    image = Image.new("RGB", (1800, 2000), "white")
    draw = ImageDraw.Draw(image)
    for y in range(100, 1850, 40):
        draw.rectangle((100, y, 1650, y + 9), fill="black")
    image.save(path, format="JPEG", quality=95)


class CliEndToEndTests(unittest.TestCase):
    def test_cli_defaults_to_multi_identifier_parser(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                image = root / "report.jpg"
                output = root / "result.json"
                config = root / "config.json"
                _report_image(image)
                config.write_text(
                    json.dumps(
                        {
                            "ocr": {"endpoint": "http://127.0.0.1:%d/ocr" % server.server_port},
                            "llm": {
                                "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % server.server_port,
                                "model": "test-model",
                            },
                        }
                    ),
                    encoding="utf-8",
                )

                code = main(
                    [
                        "--config", str(config),
                        "--image", str(image),
                        "--output", str(output),
                        "--allow-unverified-runtime",
                    ]
                )
                result = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(code, 1)
                self.assertEqual(result["status"], "review_required")
                self.assertEqual(result["primary_identifier"]["type"], "patient_id")
                self.assertNotIn("fields", result)
        finally:
            server.shutdown()
            server.server_close()

    def test_cli_uses_local_ocr_and_local_rkllm_endpoints(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                image = root / "report.jpg"
                output = root / "result.json"
                debug = root / "debug"
                config = root / "config.json"
                _report_image(image)
                config.write_text(
                    json.dumps(
                        {
                            "ocr": {"endpoint": "http://127.0.0.1:%d/ocr" % server.server_port},
                            "llm": {
                                "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % server.server_port,
                                "model": "test-model",
                            },
                        }
                    ),
                    encoding="utf-8",
                )

                code = main(
                    [
                        "--config",
                        str(config),
                        "--image",
                        str(image),
                        "--output",
                        str(output),
                        "--debug-dir",
                        str(debug),
                        "--allow-unverified-runtime",
                        "--all-fields",
                    ]
                )

                result = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(code, 0)
                self.assertEqual(result["status"], "accepted")
                self.assertEqual(result["fields"]["patient_name"]["value"], "张三")
                self.assertEqual(result["fields"]["patient_id"]["value"], "P2605260007")
                self.assertTrue((debug / "ocr_spans.json").is_file())
                self.assertTrue((debug / "ocr_overlay.jpg").is_file())
                self.assertTrue((debug / "model_response.txt").is_file())
        finally:
            server.shutdown()
            server.server_close()

    def test_cli_can_use_desktop_constrained_span_choice(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                image = root / "report.jpg"
                output = root / "result.json"
                config = root / "config.json"
                _report_image(image)
                config.write_text(
                    json.dumps(
                        {
                            "ocr": {"endpoint": "http://127.0.0.1:%d/ocr" % server.server_port},
                            "llm": {
                                "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % server.server_port,
                                "model": "test-model",
                            },
                        }
                    ),
                    encoding="utf-8",
                )

                code = main(
                    [
                        "--config",
                        str(config),
                        "--image",
                        str(image),
                        "--output",
                        str(output),
                        "--linker-mode",
                        "constrained_choice",
                        "--allow-unverified-runtime",
                        "--all-fields",
                    ]
                )

                result = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(code, 0)
                self.assertEqual(result["status"], "accepted")
                self.assertEqual(result["fields"]["report_no"]["value"], "R202608100001")
        finally:
            server.shutdown()
            server.server_close()

    def test_cli_can_use_model_selected_label_and_value_evidence(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                image = root / "report.jpg"
                output = root / "result.json"
                config = root / "config.json"
                _report_image(image)
                config.write_text(
                    json.dumps(
                        {
                            "ocr": {"endpoint": "http://127.0.0.1:%d/ocr" % server.server_port},
                            "llm": {
                                "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % server.server_port,
                                "model": "test-model",
                            },
                        }
                    ),
                    encoding="utf-8",
                )

                code = main(
                    [
                        "--config",
                        str(config),
                        "--image",
                        str(image),
                        "--output",
                        str(output),
                        "--linker-mode",
                        "evidence_choice",
                        "--association-mode",
                        "evidence",
                        "--allow-unverified-runtime",
                        "--all-fields",
                    ]
                )

                result = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(code, 0)
                self.assertEqual(result["status"], "accepted")
                self.assertEqual(result["association"]["mode"], "evidence")
                self.assertEqual(result["fields"]["patient_id"]["label_span_ids"], [3])
                self.assertEqual(result["fields"]["patient_id"]["source_span_ids"], [4])
                self.assertEqual(result["fields"]["report_no"]["label_span_ids"], [11])
        finally:
            server.shutdown()
            server.server_close()

    def test_cli_can_use_chat_selected_and_confirmed_evidence(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                image = root / "report.jpg"
                output = root / "result.json"
                config = root / "config.json"
                _report_image(image)
                config.write_text(
                    json.dumps(
                        {
                            "ocr": {"endpoint": "http://127.0.0.1:%d/ocr" % server.server_port},
                            "llm": {
                                "endpoint": "http://127.0.0.1:%d/v1/chat/completions" % server.server_port,
                                "model": "test-model",
                            },
                        }
                    ),
                    encoding="utf-8",
                )

                code = main(
                    [
                        "--config",
                        str(config),
                        "--image",
                        str(image),
                        "--output",
                        str(output),
                        "--linker-mode",
                        "evidence_chat",
                        "--association-mode",
                        "evidence",
                        "--allow-unverified-runtime",
                        "--all-fields",
                    ]
                )

                result = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(code, 0)
                self.assertEqual(result["status"], "accepted")
                self.assertEqual(result["fields"]["patient_name"]["label_span_ids"], [1])
                self.assertEqual(result["fields"]["patient_id"]["source_span_ids"], [4])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
