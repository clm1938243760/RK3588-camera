from __future__ import annotations

import json
import threading
import unittest
from urllib.request import Request, urlopen

from rk3588_report_parser.uie_web_server import create_server


class FakeService:
    def __init__(self):
        self.result = None
        self.schema = {
            "schema_version": 1,
            "model": "uie-base",
            "fields": [{"field_key": "patient_name", "prompt": "患者姓名", "minimum_probability": 0.5}],
        }

    def runtime_summary(self):
        return {"model": "uie-base", "ocr_backend": "local_ppocr", "field_count": 1}

    def get_schema(self):
        return self.schema

    def update_schema(self, payload):
        self.schema = {"schema_version": 1, "model": "uie-base", "fields": payload["fields"]}
        return self.schema

    def latest(self):
        return self.result

    def parse_image(self, image_bytes):
        self.result = result_payload("upload-1")
        return self.result

    def parse_capture(self, payload):
        self.result = result_payload(payload["capture_id"])
        return self.result

    def select_candidate(self, field_key, candidate_index):
        self.result["selected_candidate"] = {"field_key": field_key, "candidate_index": candidate_index}
        return self.result


def result_payload(capture_id):
    return {
        "status": "accepted",
        "capture_id": capture_id,
        "patient_response": {
            "code": "SUCCESS",
            "data": [{"patient_name": "张三"}],
            "msg": "成功",
            "success": True,
        },
        "fields": {},
        "document": {"blocks": [], "full_text": ""},
        "timings": {"total_ms": 1},
    }


def multipart_body(image=b"fake-jpeg"):
    boundary = "----uie-web-test"
    body = (
        "--%s\r\n" % boundary
        + 'Content-Disposition: form-data; name="image"; filename="report.jpg"\r\n'
        + "Content-Type: image/jpeg\r\n\r\n"
    ).encode("ascii") + image + ("\r\n--%s--\r\n" % boundary).encode("ascii")
    return boundary, body


class UieWebServerTests(unittest.TestCase):
    def test_ui_schema_parse_patient_and_camera_endpoints(self) -> None:
        service = FakeService()
        server = create_server("127.0.0.1", 0, service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_port
        try:
            with urlopen(base + "/", timeout=3) as response:
                self.assertIn("患者信息结构化", response.read().decode("utf-8"))
            with urlopen(base + "/api/v1/patient", timeout=3) as response:
                empty = json.loads(response.read().decode("utf-8"))
            self.assertEqual(empty["code"], "NO_RESULT")

            boundary, body = multipart_body()
            parse_request = Request(
                base + "/api/v1/parse",
                data=body,
                method="POST",
                headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary},
            )
            with urlopen(parse_request, timeout=3) as response:
                parsed = json.loads(response.read().decode("utf-8"))
            self.assertEqual(parsed["capture_id"], "upload-1")

            camera_request = Request(
                base + "/internal/v1/uie/extract",
                data=json.dumps({"capture_id": "camera-1"}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urlopen(camera_request, timeout=3) as response:
                camera = json.loads(response.read().decode("utf-8"))
            self.assertEqual(camera["capture_id"], "camera-1")

            select_request = Request(
                base + "/api/v1/result/select",
                data=json.dumps({"field_key": "patient_id", "candidate_index": 1}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urlopen(select_request, timeout=3) as response:
                selected = json.loads(response.read().decode("utf-8"))
            self.assertEqual(selected["selected_candidate"]["candidate_index"], 1)
            with urlopen(base + "/api/v1/patient", timeout=3) as response:
                patient = json.loads(response.read().decode("utf-8"))
            self.assertEqual(patient["data"][0]["patient_name"], "张三")

            schema_request = Request(
                base + "/api/v1/schema",
                data=json.dumps({"fields": [{"field_key": "patient_name", "prompt": "病人姓名"}]}).encode("utf-8"),
                method="PUT",
                headers={"Content-Type": "application/json"},
            )
            with urlopen(schema_request, timeout=3) as response:
                configured = json.loads(response.read().decode("utf-8"))
            self.assertEqual(configured["fields"][0]["prompt"], "病人姓名")
        finally:
            server.shutdown()
            server.server_close()

    def test_non_loopback_listener_requires_token(self) -> None:
        with self.assertRaises(ValueError):
            create_server("0.0.0.0", 0, FakeService())


if __name__ == "__main__":
    unittest.main()
