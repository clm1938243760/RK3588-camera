from __future__ import annotations

import threading
import time
import unittest

from rk3588_report_parser.capture_text_runtime import (
    LatestOnlyOcrWorker,
)


class LatestOnlyOcrWorkerTests(unittest.TestCase):
    def test_only_latest_pending_job_is_retained(self) -> None:
        started = threading.Event()
        release = threading.Event()
        executed = []

        def first():
            started.set()
            release.wait(2)
            executed.append("first")
            return "first-result"

        worker = LatestOnlyOcrWorker()
        try:
            worker.submit("capture-a", first)
            self.assertTrue(started.wait(1))
            worker.submit("capture-b", lambda: executed.append("middle"))
            worker.submit("capture-c", lambda: executed.append("latest") or "latest-result")
            release.set()

            completed = None
            deadline = time.monotonic() + 2
            while completed is None and time.monotonic() < deadline:
                completed = worker.poll()
                time.sleep(0.01)
            self.assertEqual(completed[:2], ("capture-a", "first-result"))

            latest = None
            while latest is None and time.monotonic() < deadline:
                latest = worker.poll()
                time.sleep(0.01)
            self.assertEqual(latest[:2], ("capture-c", "latest-result"))
            self.assertEqual(executed, ["first", "latest"])
        finally:
            worker.close()

if __name__ == "__main__":
    unittest.main()
