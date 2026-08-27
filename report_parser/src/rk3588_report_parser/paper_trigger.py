"""Document-presence tracking used before expensive OCR work.

The tracker is deliberately independent from DocAligner and OpenCV. A paper
detector supplies four corners, and this module decides when the same paper has
remained still long enough to trigger one capture cycle.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence, Tuple


Point = Tuple[float, float]
BBox = Tuple[float, float, float, float]


class PaperState(str, Enum):
    ABSENT = "absent"
    TRACKING = "tracking"
    STABLE = "stable"
    LOCKED = "locked"


@dataclass(frozen=True)
class PaperTrackerConfig:
    stable_seconds: float = 0.5
    min_observations: int = 3
    min_iou: float = 0.90
    max_center_shift_ratio: float = 0.03
    max_area_change_ratio: float = 0.15
    remove_seconds: float = 0.5

    def __post_init__(self) -> None:
        if self.stable_seconds <= 0:
            raise ValueError("stable_seconds must be greater than zero")
        if self.min_observations < 2:
            raise ValueError("min_observations must be at least two")
        if not 0 < self.min_iou <= 1:
            raise ValueError("min_iou must be in the range (0, 1]")
        if not 0 <= self.max_center_shift_ratio <= 1:
            raise ValueError("max_center_shift_ratio must be in the range [0, 1]")
        if self.max_area_change_ratio < 0:
            raise ValueError("max_area_change_ratio must be zero or greater")
        if self.remove_seconds < 0:
            raise ValueError("remove_seconds must be zero or greater")


@dataclass(frozen=True)
class PaperObservation:
    timestamp: float
    corners: Tuple[Point, Point, Point, Point]
    frame_width: int
    frame_height: int
    confidence: Optional[float] = None

    @classmethod
    def from_corners(
        cls,
        timestamp: float,
        corners: Sequence[Sequence[float]],
        frame_width: int,
        frame_height: int,
        confidence: Optional[float] = None,
    ) -> "PaperObservation":
        if frame_width <= 0 or frame_height <= 0:
            raise ValueError("frame dimensions must be greater than zero")
        if len(corners) != 4:
            raise ValueError("exactly four document corners are required")
        converted = tuple((float(point[0]), float(point[1])) for point in corners)
        for x, y in converted:
            if not math.isfinite(x) or not math.isfinite(y):
                raise ValueError("corner coordinates must be finite")
        return cls(
            timestamp=float(timestamp),
            corners=converted,  # type: ignore[arg-type]
            frame_width=int(frame_width),
            frame_height=int(frame_height),
            confidence=confidence,
        )

    @property
    def normalized_bbox(self) -> BBox:
        xs = [point[0] / self.frame_width for point in self.corners]
        ys = [point[1] / self.frame_height for point in self.corners]
        return min(xs), min(ys), max(xs), max(ys)

    @property
    def normalized_center(self) -> Point:
        left, top, right, bottom = self.normalized_bbox
        return (left + right) / 2.0, (top + bottom) / 2.0

    @property
    def normalized_area(self) -> float:
        area = 0.0
        for index, (x1, y1) in enumerate(self.corners):
            x2, y2 = self.corners[(index + 1) % len(self.corners)]
            area += x1 * y2 - x2 * y1
        return abs(area) / 2.0 / (self.frame_width * self.frame_height)


@dataclass(frozen=True)
class PaperTrackerUpdate:
    state: PaperState
    triggered: bool
    reason: str
    stable_for: float
    observations: int
    iou: Optional[float] = None
    center_shift_ratio: Optional[float] = None
    area_change_ratio: Optional[float] = None


def bbox_iou(first: BBox, second: BBox) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    first_area = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
    second_area = max(0.0, second[2] - second[0]) * max(0.0, second[3] - second[1])
    union = first_area + second_area - intersection
    return intersection / union if union > 0 else 0.0


class PaperStabilityTracker:
    """Emit one trigger after a document remains geometrically stable."""

    def __init__(self, config: Optional[PaperTrackerConfig] = None) -> None:
        self.config = config or PaperTrackerConfig()
        self._state = PaperState.ABSENT
        self._anchor: Optional[PaperObservation] = None
        self._tracking_since: Optional[float] = None
        self._missing_since: Optional[float] = None
        self._observations = 0
        self._last_timestamp: Optional[float] = None

    @property
    def state(self) -> PaperState:
        return self._state

    def reset(self) -> None:
        self._state = PaperState.ABSENT
        self._anchor = None
        self._tracking_since = None
        self._missing_since = None
        self._observations = 0
        self._last_timestamp = None

    def update(
        self,
        observation: Optional[PaperObservation],
        timestamp: Optional[float] = None,
    ) -> PaperTrackerUpdate:
        now = observation.timestamp if observation is not None else timestamp
        if now is None:
            now = time.monotonic()
        now = float(now)
        if self._last_timestamp is not None and now < self._last_timestamp:
            raise ValueError("observations must be supplied in timestamp order")
        self._last_timestamp = now

        if observation is None:
            return self._update_missing(now)
        if self._state is PaperState.LOCKED:
            self._missing_since = None
            return self._result(PaperState.LOCKED, False, "waiting_for_paper_removal", now)
        if self._anchor is None or self._tracking_since is None:
            self._start_tracking(observation)
            return self._result(PaperState.TRACKING, False, "paper_acquired", now)

        iou, center_shift, area_change = self._geometry_delta(self._anchor, observation)
        if not self._geometry_is_stable(iou, center_shift, area_change):
            self._start_tracking(observation)
            return self._result(
                PaperState.TRACKING,
                False,
                "paper_moved",
                now,
                iou,
                center_shift,
                area_change,
            )

        self._observations += 1
        stable_for = now - self._tracking_since
        if (
            stable_for + 1e-9 >= self.config.stable_seconds
            and self._observations >= self.config.min_observations
        ):
            self._state = PaperState.LOCKED
            return self._result(
                PaperState.STABLE,
                True,
                "paper_stable",
                now,
                iou,
                center_shift,
                area_change,
            )
        return self._result(
            PaperState.TRACKING,
            False,
            "stability_pending",
            now,
            iou,
            center_shift,
            area_change,
        )

    def _update_missing(self, now: float) -> PaperTrackerUpdate:
        if self._state is not PaperState.LOCKED:
            self._clear_tracking()
            return self._result(PaperState.ABSENT, False, "paper_not_detected", now)

        if self._missing_since is None:
            self._missing_since = now
        if now - self._missing_since >= self.config.remove_seconds:
            self._clear_tracking()
            return self._result(PaperState.ABSENT, False, "paper_removed", now)
        return self._result(PaperState.LOCKED, False, "paper_removal_pending", now)

    def _start_tracking(self, observation: PaperObservation) -> None:
        self._state = PaperState.TRACKING
        self._anchor = observation
        self._tracking_since = observation.timestamp
        self._missing_since = None
        self._observations = 1

    def _clear_tracking(self) -> None:
        self._state = PaperState.ABSENT
        self._anchor = None
        self._tracking_since = None
        self._missing_since = None
        self._observations = 0

    def _geometry_delta(
        self,
        anchor: PaperObservation,
        current: PaperObservation,
    ) -> Tuple[float, float, float]:
        iou = bbox_iou(anchor.normalized_bbox, current.normalized_bbox)
        anchor_center = anchor.normalized_center
        current_center = current.normalized_center
        center_shift = math.hypot(
            current_center[0] - anchor_center[0],
            current_center[1] - anchor_center[1],
        )
        if anchor.normalized_area <= 0:
            area_change = math.inf
        else:
            area_change = abs(current.normalized_area - anchor.normalized_area) / anchor.normalized_area
        return iou, center_shift, area_change

    def _geometry_is_stable(self, iou: float, center_shift: float, area_change: float) -> bool:
        return (
            iou >= self.config.min_iou
            and center_shift <= self.config.max_center_shift_ratio
            and area_change <= self.config.max_area_change_ratio
        )

    def _result(
        self,
        state: PaperState,
        triggered: bool,
        reason: str,
        now: float,
        iou: Optional[float] = None,
        center_shift: Optional[float] = None,
        area_change: Optional[float] = None,
    ) -> PaperTrackerUpdate:
        stable_for = 0.0
        if self._tracking_since is not None:
            stable_for = max(0.0, now - self._tracking_since)
        return PaperTrackerUpdate(
            state=state,
            triggered=triggered,
            reason=reason,
            stable_for=stable_for,
            observations=self._observations,
            iou=iou,
            center_shift_ratio=center_shift,
            area_change_ratio=area_change,
        )
