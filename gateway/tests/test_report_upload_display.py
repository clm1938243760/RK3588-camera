from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rk3588_gateway.report_upload import ReportUploadWorker
from rk3588_gateway.workflow import GatewayWorkflow


class RecordingQueue:
    def __init__(self) -> None:
        self.events = []

    def put(self, event) -> None:
        self.events.append(event)


class ReportUploadDisplayTests(unittest.TestCase):
    def test_legacy_worker_publishes_uploading_before_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            watch_dir = root / "reports"
            state_dir = root / "state"
            watch_dir.mkdir()
            state_dir.mkdir()
            pdf = watch_dir / "report.pdf"
            pdf.write_bytes(b"%PDF-test")
            report_info = root / "ReportInfo.xml"
            report_info.write_text("<ReportInfo />", encoding="utf-8")
            display_path = root / "display-state.json"
            config = SimpleNamespace(
                report_info_path=str(report_info),
                state_dir=str(state_dir),
                max_attempts=3,
                retry_interval_seconds=1,
            )
            pdf_config = SimpleNamespace(output_dir=str(watch_dir))
            queue = RecordingQueue()
            worker = ReportUploadWorker(
                config,
                pdf_config,
                queue,
                "test-device",
                display_state_path=str(display_path),
            )

            with mock.patch.object(worker, "_upload", return_value=(True, "", "OK")):
                worker._scan_once()

            self.assertEqual(
                [event.type for event in queue.events],
                ["report.uploading", "report.uploaded"],
            )
            state = json.loads(display_path.read_text(encoding="utf-8"))
            self.assertEqual(state["display"]["screen"], "report_upload_success")
            self.assertIn("expires_at", state)

    def test_workflow_maps_uploading_event_to_uploading_screen(self) -> None:
        workflow = GatewayWorkflow.__new__(GatewayWorkflow)
        workflow._handled_report_events = set()
        workflow._started_at = 0.0
        calls = []
        workflow._set_display = lambda *args, **kwargs: calls.append((args, kwargs))

        changed = workflow.handle_report_upload(
            "report.uploading",
            "/tmp/report.pdf",
            event_id="uploading-1",
        )

        self.assertTrue(changed)
        self.assertEqual(calls[0][0][0], "report_uploading")


if __name__ == "__main__":
    unittest.main()
