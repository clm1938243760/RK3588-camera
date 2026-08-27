from __future__ import annotations

import base64
import json
import threading
import unittest
from urllib.request import Request, urlopen

from rk3588_report_parser.desktop_ppocr_server import create_server, normalize_paddle_result


class FakeOcrEngine:
    def __init__(self) -> None:
        self.images = []

    def recognize(self, image_bytes):
        self.images.append(image_bytes)
        return [
            {
                "text": "Alice",
                "score": 0.99,
                "box": [10, 20, 120, 50],
                "polygon": [[10, 20], [120, 20], [120, 50], [10, 50]],
            }
        ]


class DesktopPpOcrServerTests(unittest.TestCase):
    def test_normalizes_paddle_v2_polygon_text_score_result(self) -> None:
        raw = [
            [
                [
                    [[10, 20], [120, 20], [120, 50], [10, 50]],
                    ("Alice", 0.99),
                ],
                [
                    [[160, 20], [260, 20], [260, 50], [160, 50]],
                    ("P2605260007", 0.97),
                ],
            ]
        ]

        items = normalize_paddle_result(raw)

        self.assertEqual([item["text"] for item in items], ["Alice", "P2605260007"])
        self.assertEqual(items[0]["box"], [10.0, 20.0, 120.0, 50.0])
        self.assertEqual(items[1]["polygon"][0], [160.0, 20.0])

    def test_loopback_server_exposes_health_and_ocr_contract(self) -> None:
        engine = FakeOcrEngine()
        server = create_server("127.0.0.1", 0, engine)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:%d" % server.server_port
            with urlopen(base + "/health", timeout=3) as response:
                health = json.loads(response.read().decode("utf-8"))
            self.assertEqual(health, {"ok": True, "backend": "paddleocr_desktop", "local_only": True})

            image = b"synthetic-image-bytes"
            payload = {"image_base64": base64.b64encode(image).decode("ascii")}
            request = Request(
                base + "/ocr",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=3) as response:
                result = json.loads(response.read().decode("utf-8"))
            self.assertTrue(result["ok"])
            self.assertEqual(result["ocr"][0]["text"], "Alice")
            self.assertEqual(engine.images, [image])
        finally:
            server.shutdown()
            server.server_close()

    def test_server_refuses_non_loopback_binding(self) -> None:
        with self.assertRaises(ValueError):
            create_server("0.0.0.0", 5002, FakeOcrEngine())


if __name__ == "__main__":
    unittest.main()
