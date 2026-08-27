from __future__ import annotations

import json
import threading
import unittest
from urllib.request import Request, urlopen

from rk3588_report_parser.desktop_server import create_server


class FakeEngine:
    def __init__(self) -> None:
        self.calls = []
        self.choice_calls = []

    def generate(self, messages, max_tokens):
        self.calls.append((messages, max_tokens))
        return "{\"patient_name\":{\"span_ids\":[2]}}"

    def choose(self, messages, allowed_ids):
        self.choice_calls.append((messages, allowed_ids))
        return 2


class DesktopServerTests(unittest.TestCase):
    def test_local_server_exposes_health_and_chat_without_logging_prompts(self) -> None:
        engine = FakeEngine()
        server = create_server("127.0.0.1", 0, engine, "desktop-test")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:%d" % server.server_port
            with urlopen(base + "/health", timeout=3) as response:
                health = json.loads(response.read().decode("utf-8"))
            self.assertEqual(health, {"ok": True, "model": "desktop-test", "local_only": True})

            payload = {
                "model": "desktop-test",
                "messages": [{"role": "user", "content": "sensitive OCR text"}],
                "max_tokens": 64,
            }
            request = Request(
                base + "/v1/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=3) as response:
                result = json.loads(response.read().decode("utf-8"))
            self.assertEqual(result["choices"][0]["message"]["content"], "{\"patient_name\":{\"span_ids\":[2]}}")
            self.assertEqual(engine.calls[0][0], payload["messages"])
            self.assertEqual(engine.calls[0][1], 64)
        finally:
            server.shutdown()
            server.server_close()

    def test_server_refuses_non_loopback_binding(self) -> None:
        with self.assertRaises(ValueError):
            create_server("0.0.0.0", 8010, FakeEngine(), "desktop-test")

    def test_local_server_exposes_constrained_span_choice(self) -> None:
        engine = FakeEngine()
        server = create_server("127.0.0.1", 0, engine, "desktop-test")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:%d" % server.server_port
            payload = {
                "model": "desktop-test",
                "messages": [{"role": "user", "content": "sensitive OCR text"}],
                "allowed_ids": [0, 1, 2],
            }
            request = Request(
                base + "/v1/span-choice",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=3) as response:
                result = json.loads(response.read().decode("utf-8"))
            self.assertEqual(result["choice_id"], 2)
            self.assertEqual(engine.choice_calls[0][0], payload["messages"])
            self.assertEqual(engine.choice_calls[0][1], payload["allowed_ids"])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
