from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from rk3588_report_parser.report_center_forwarder import (
    CameraCaptureForwarder,
    ensure_loopback_endpoint,
)


def capture_payload():
    return {
        "capture_id": "capture-001",
        "status": "accepted",
        "created_at": 1787190000.0,
        "source": {"selected_frame_sha256": "a" * 64},
        "quality": {},
        "document": {
            "schema_version": 2,
            "full_text": "private patient text",
            "lines": [{"id": 1, "text": "private patient text"}],
            "blocks": [{"id": 1, "text": "private patient text", "score": 0.9}],
        },
    }


class CameraCaptureForwarderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "result.json"
        self.state = self.root / "state.json"
        self.source.write_text(json.dumps(capture_payload()), encoding="utf-8")
        self.calls = []

    def tearDown(self) -> None:
        self.temp.cleanup()

    def sender(self, endpoint, payload, timeout, insecure):
        self.calls.append((endpoint, payload["capture_id"], timeout, insecure))
        return {"ok": True, "created": True}

    def test_forwards_once_and_persists_only_non_phi_state(self) -> None:
        forwarder = CameraCaptureForwarder(
            str(self.source), str(self.state),
            "https://127.0.0.1:8443/internal/v1/camera-captures",
            allow_insecure_loopback_tls=True,
            sender=self.sender,
        )
        self.assertEqual(forwarder.run_once(), "forwarded")
        self.assertEqual(forwarder.run_once(), "unchanged")
        self.assertEqual(len(self.calls), 1)
        state_text = self.state.read_text(encoding="utf-8")
        self.assertNotIn("private patient text", state_text)
        if os.name != "nt":
            self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)

    def test_new_capture_is_forwarded(self) -> None:
        forwarder = CameraCaptureForwarder(
            str(self.source), str(self.state), "http://localhost:8443/internal/v1/camera-captures",
            sender=self.sender,
        )
        self.assertEqual(forwarder.run_once(), "forwarded")
        payload = capture_payload()
        payload["capture_id"] = "capture-002"
        self.source.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(forwarder.run_once(), "forwarded")
        self.assertEqual(len(self.calls), 2)

    def test_non_loopback_endpoint_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ensure_loopback_endpoint("https://192.0.2.10:8443/internal/v1/camera-captures")


if __name__ == "__main__":
    unittest.main()
