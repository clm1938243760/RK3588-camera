from __future__ import annotations

import unittest

from rk3588_report_parser.capture_trigger import TriggeredOcrController
from rk3588_report_parser.paper_detector import PaperDetection
from rk3588_report_parser.paper_trigger import PaperStabilityTracker, PaperTrackerConfig


CORNERS = ((100.0, 100.0), (900.0, 100.0), (900.0, 900.0), (100.0, 900.0))


class FakeDetector:
    def detect_jpeg(self, image_bytes: bytes) -> PaperDetection:
        corners = () if image_bytes == b"missing" else CORNERS
        return PaperDetection(
            corners=corners,
            frame_width=1000,
            frame_height=1000,
            confidence=0.95 if corners else 0.1,
            inference_ms=3.0,
        )


def tracker() -> PaperStabilityTracker:
    return PaperStabilityTracker(
        PaperTrackerConfig(stable_seconds=0.8, min_observations=3, remove_seconds=0.5)
    )


class TriggeredOcrControllerTests(unittest.TestCase):
    def test_ocr_runs_once_after_stability_and_rearms_after_removal(self) -> None:
        calls = []

        def run_ocr(image_bytes: bytes):
            calls.append(image_bytes)
            return {"ok": True, "ocr": [{"text": "123456", "box": [1, 2, 3, 4]}]}

        controller = TriggeredOcrController(FakeDetector(), tracker(), run_ocr)
        self.assertFalse(controller.process_frame(b"paper", 0.0).ocr_attempted)
        self.assertFalse(controller.process_frame(b"paper", 0.4).ocr_attempted)
        triggered = controller.process_frame(b"paper", 0.8)
        self.assertTrue(triggered.ocr_attempted)
        self.assertTrue(triggered.ocr_available)
        self.assertTrue(triggered.text_detected)
        self.assertEqual(triggered.ocr_count, 1)
        self.assertEqual(len(calls), 1)

        held = controller.process_frame(b"paper", 1.0)
        self.assertFalse(held.ocr_attempted)
        self.assertTrue(held.ocr_available)
        self.assertTrue(held.text_detected)
        controller.process_frame(b"missing", 1.1)
        removed = controller.process_frame(b"missing", 1.6)
        self.assertFalse(removed.ocr_available)
        controller.process_frame(b"paper", 1.7)
        controller.process_frame(b"paper", 2.1)
        self.assertTrue(controller.process_frame(b"paper", 2.5).ocr_attempted)
        self.assertEqual(len(calls), 2)

    def test_empty_ocr_result_is_reported_without_patient_text(self) -> None:
        controller = TriggeredOcrController(
            FakeDetector(),
            tracker(),
            lambda image: {"ok": True, "ocr": []},
        )
        controller.process_frame(b"paper", 0.0)
        controller.process_frame(b"paper", 0.4)
        result = controller.process_frame(b"paper", 0.8)
        public = result.public_status()

        self.assertTrue(result.ocr_attempted)
        self.assertTrue(result.ocr_available)
        self.assertFalse(result.text_detected)
        self.assertEqual(result.ocr_count, 0)
        self.assertNotIn("ocr_items", public)
        self.assertEqual(public["frame_size"], {"width": 1000, "height": 1000})
        self.assertEqual(public["paper_corners"], [list(point) for point in CORNERS])

    def test_ocr_failure_is_captured_and_not_retried_while_locked(self) -> None:
        calls = []

        def fail(image: bytes):
            calls.append(image)
            raise RuntimeError("service unavailable")

        controller = TriggeredOcrController(FakeDetector(), tracker(), fail)
        controller.process_frame(b"paper", 0.0)
        controller.process_frame(b"paper", 0.4)
        failed = controller.process_frame(b"paper", 0.8)
        held = controller.process_frame(b"paper", 1.0)

        self.assertEqual(failed.ocr_error, "service unavailable")
        self.assertFalse(held.ocr_attempted)
        self.assertTrue(held.ocr_available)
        self.assertEqual(held.ocr_error, "service unavailable")
        self.assertEqual(len(calls), 1)

    def test_optional_preprocessor_receives_detection_before_ocr(self) -> None:
        processed = []

        def prepare(image: bytes, detection: PaperDetection) -> bytes:
            processed.append((image, detection.corners))
            return b"prepared-document"

        def run_ocr(image: bytes):
            self.assertEqual(image, b"prepared-document")
            return {"ok": True, "ocr": [{"text": "123456", "box": [1, 2, 3, 4]}]}

        controller = TriggeredOcrController(
            FakeDetector(),
            tracker(),
            run_ocr,
            ocr_image_preprocessor=prepare,
        )
        controller.process_frame(b"paper", 0.0)
        controller.process_frame(b"paper", 0.4)
        result = controller.process_frame(b"paper", 0.8)

        self.assertTrue(result.text_detected)
        self.assertEqual(processed, [(b"paper", CORNERS)])


if __name__ == "__main__":
    unittest.main()
