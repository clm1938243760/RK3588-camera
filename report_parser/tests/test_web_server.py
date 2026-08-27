from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from rk3588_report_parser.settings import load_settings
from rk3588_report_parser.web_server import ParserService, create_server


class FakeService:
    def __init__(self):
        self.rules = {"enabled": False, "profile": "test", "fields": []}

    def parse(self, image_bytes):
        return {
            "status": "accepted",
            "primary_identifier": {"type": "patient_id", "value": "P20260001"},
            "identifiers": [],
            "alternatives": [],
            "quality": {},
            "timings": {"total_ms": 10},
            "engine": {},
            "image_sha256": "hash",
        }

    def get_rules(self):
        return self.rules

    def rule_summary(self):
        return {"enabled": self.rules["enabled"], "profile": self.rules["profile"], "field_count": len(self.rules["fields"])}

    def update_rules(self, payload):
        self.rules = payload
        return payload


def multipart_body(image=b"fake-jpeg"):
    boundary = "----report-parser-test"
    body = (
        "--%s\r\n" % boundary
        + 'Content-Disposition: form-data; name="image"; filename="report.jpg"\r\n'
        + "Content-Type: image/jpeg\r\n\r\n"
    ).encode("ascii") + image + ("\r\n--%s--\r\n" % boundary).encode("ascii")
    return boundary, body


class WebServerTests(unittest.TestCase):
    def test_parser_service_validates_and_persists_rules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rules_path = Path(directory) / "active_rules.json"
            parser = SimpleNamespace(settings=load_settings())
            service = ParserService(parser, rules_path=rules_path)
            configured = service.update_rules(
                {
                    "enabled": True,
                    "profile": "hospital-a",
                    "fields": [
                        {"type": "patient_id", "lengths": [11], "charset": "digits", "priority": 100}
                    ],
                }
            )

            self.assertTrue(configured["enabled"])
            self.assertTrue(rules_path.is_file())
            self.assertEqual(parser.settings.identifier_rules.profile, "hospital-a")

    def test_serves_ui_parse_and_rule_configuration_api(self) -> None:
        runtime = {"profile": "pc-12g", "model": "test", "ok": True}
        server = create_server("127.0.0.1", 0, FakeService(), runtime)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_port
        try:
            with urlopen(base + "/", timeout=3) as response:
                html = response.read().decode("utf-8")
            self.assertIn("申请单号码提取", html)
            with urlopen(base + "/favicon.ico", timeout=3) as response:
                self.assertEqual(response.status, 204)
            boundary, body = multipart_body()
            request = Request(
                base + "/api/v1/parse",
                data=body,
                method="POST",
                headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary},
            )
            with urlopen(request, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(payload["primary_identifier"]["value"], "P20260001")

            rules = {
                "enabled": True,
                "profile": "hospital-a",
                "fields": [
                    {"type": "patient_id", "lengths": [11], "charset": "digits", "prefixes": [], "priority": 100, "enabled": True}
                ],
            }
            rule_request = Request(
                base + "/api/v1/rules",
                data=json.dumps(rules).encode("utf-8"),
                method="PUT",
                headers={"Content-Type": "application/json"},
            )
            with urlopen(rule_request, timeout=3) as response:
                configured = json.loads(response.read().decode("utf-8"))
            self.assertTrue(configured["enabled"])
            with urlopen(base + "/api/v1/rules", timeout=3) as response:
                loaded = json.loads(response.read().decode("utf-8"))
            self.assertEqual(loaded["profile"], "hospital-a")
        finally:
            server.shutdown()
            server.server_close()

    def test_non_loopback_requires_token_and_token_is_enforced(self) -> None:
        runtime = {"profile": "pc-12g"}
        with self.assertRaises(ValueError):
            create_server("0.0.0.0", 0, FakeService(), runtime)

        server = create_server("127.0.0.1", 0, FakeService(), runtime, "secret")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_port
        try:
            with urlopen(base + "/", timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("申请单号码提取", response.read().decode("utf-8"))
            with urlopen(base + "/api/v1/health", timeout=3) as response:
                self.assertEqual(response.status, 200)
            with self.assertRaises(HTTPError) as caught:
                urlopen(base + "/api/v1/runtime", timeout=3)
            self.assertEqual(caught.exception.code, 401)
            authorized = Request(base + "/api/v1/runtime", headers={"Authorization": "Bearer secret"})
            with urlopen(authorized, timeout=3) as response:
                self.assertEqual(response.status, 200)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
