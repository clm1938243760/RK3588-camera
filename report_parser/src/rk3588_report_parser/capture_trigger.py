"""Coordinate low-cost paper detection with one-shot OCR triggering."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Protocol

from .paper_detector import PaperDetection
from .paper_trigger import PaperObservation, PaperStabilityTracker, PaperTrackerUpdate


class PaperDetectorProtocol(Protocol):
    def detect_jpeg(self, image_bytes: bytes) -> PaperDetection:
        ...


OcrCallback = Callable[[bytes], Dict[str, Any]]
OcrImagePreprocessor = Callable[[bytes, PaperDetection], bytes]


@dataclass(frozen=True)
class CaptureTriggerResult:
    detection: PaperDetection
    tracker: PaperTrackerUpdate
    ocr_attempted: bool = False
    ocr_available: bool = False
    text_detected: bool = False
    ocr_count: int = 0
    ocr_elapsed_ms: Optional[float] = None
    ocr_error: Optional[str] = None

    def public_status(self) -> Dict[str, Any]:
        corners = [
            [round(float(x), 2), round(float(y), 2)]
            for x, y in self.detection.corners
        ]
        return {
            "paper_detected": self.detection.detected,
            "paper_confidence": round(self.detection.confidence, 4),
            "paper_inference_ms": round(self.detection.inference_ms, 2),
            "frame_size": {
                "width": self.detection.frame_width,
                "height": self.detection.frame_height,
            },
            "paper_corners": corners,
            "state": self.tracker.state.value,
            "reason": self.tracker.reason,
            "stable_for": round(self.tracker.stable_for, 3),
            "observations": self.tracker.observations,
            "triggered": self.tracker.triggered,
            "ocr_attempted": self.ocr_attempted,
            "ocr_available": self.ocr_available,
            "text_detected": self.text_detected,
            "ocr_count": self.ocr_count,
            "ocr_elapsed_ms": None if self.ocr_elapsed_ms is None else round(self.ocr_elapsed_ms, 2),
            "ocr_error": self.ocr_error,
        }


class TriggeredOcrController:
    def __init__(
        self,
        detector: PaperDetectorProtocol,
        tracker: PaperStabilityTracker,
        ocr_callback: OcrCallback,
        ocr_image_preprocessor: Optional[OcrImagePreprocessor] = None,
    ) -> None:
        self.detector = detector
        self.tracker = tracker
        self.ocr_callback = ocr_callback
        self.ocr_image_preprocessor = ocr_image_preprocessor
        self._ocr_available = False
        self._text_detected = False
        self._ocr_count = 0
        self._ocr_elapsed_ms: Optional[float] = None
        self._ocr_error: Optional[str] = None

    def process_frame(self, image_bytes: bytes, timestamp: float) -> CaptureTriggerResult:
        detection = self.detector.detect_jpeg(image_bytes)
        observation = None
        if detection.detected:
            observation = PaperObservation.from_corners(
                timestamp=timestamp,
                corners=detection.corners,
                frame_width=detection.frame_width,
                frame_height=detection.frame_height,
                confidence=detection.confidence,
            )
        update = self.tracker.update(observation, timestamp=timestamp)
        if update.reason in {"paper_removed", "paper_not_detected", "paper_acquired"}:
            self._clear_ocr_result()
        if not update.triggered:
            return self._result(detection, update, ocr_attempted=False)

        started = time.perf_counter()
        try:
            ocr_image = (
                self.ocr_image_preprocessor(image_bytes, detection)
                if self.ocr_image_preprocessor is not None
                else image_bytes
            )
            payload = self.ocr_callback(ocr_image)
            if not isinstance(payload, dict):
                raise ValueError("OCR callback must return an object")
            raw_items = payload.get("ocr")
            if not isinstance(raw_items, list):
                raise ValueError("OCR response is missing ocr items")
            items = [item for item in raw_items if isinstance(item, dict)]
            text_detected = any(str(item.get("text") or "").strip() for item in items)
            self._ocr_available = True
            self._text_detected = text_detected
            self._ocr_count = len(items)
            self._ocr_elapsed_ms = (time.perf_counter() - started) * 1000
            self._ocr_error = None
            return self._result(detection, update, ocr_attempted=True)
        except Exception as exc:
            self._ocr_available = True
            self._text_detected = False
            self._ocr_count = 0
            self._ocr_elapsed_ms = (time.perf_counter() - started) * 1000
            self._ocr_error = str(exc)
            return self._result(detection, update, ocr_attempted=True)

    def _clear_ocr_result(self) -> None:
        self._ocr_available = False
        self._text_detected = False
        self._ocr_count = 0
        self._ocr_elapsed_ms = None
        self._ocr_error = None

    def _result(
        self,
        detection: PaperDetection,
        update: PaperTrackerUpdate,
        ocr_attempted: bool,
    ) -> CaptureTriggerResult:
        return CaptureTriggerResult(
            detection=detection,
            tracker=update,
            ocr_attempted=ocr_attempted,
            ocr_available=self._ocr_available,
            text_detected=self._text_detected,
            ocr_count=self._ocr_count,
            ocr_elapsed_ms=self._ocr_elapsed_ms,
            ocr_error=self._ocr_error,
        )
