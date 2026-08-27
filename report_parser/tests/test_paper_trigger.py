from __future__ import annotations

import unittest

from rk3588_report_parser.paper_trigger import (
    PaperObservation,
    PaperStabilityTracker,
    PaperState,
    PaperTrackerConfig,
    bbox_iou,
)


BASE_CORNERS = ((100, 100), (900, 100), (900, 900), (100, 900))


def observation(
    timestamp: float,
    corners=BASE_CORNERS,
    width: int = 1000,
    height: int = 1000,
) -> PaperObservation:
    return PaperObservation.from_corners(timestamp, corners, width, height)


class PaperTriggerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tracker = PaperStabilityTracker(
            PaperTrackerConfig(
                stable_seconds=0.8,
                min_observations=3,
                min_iou=0.90,
                max_center_shift_ratio=0.03,
                max_area_change_ratio=0.15,
                remove_seconds=0.5,
            )
        )

    def test_absent_frame_does_not_trigger(self) -> None:
        result = self.tracker.update(None, timestamp=0.0)
        self.assertEqual(result.state, PaperState.ABSENT)
        self.assertFalse(result.triggered)

    def test_same_paper_triggers_after_time_and_sample_gate(self) -> None:
        first = self.tracker.update(observation(0.0))
        second = self.tracker.update(observation(0.4))
        third = self.tracker.update(observation(0.8))

        self.assertEqual(first.state, PaperState.TRACKING)
        self.assertFalse(second.triggered)
        self.assertEqual(third.state, PaperState.STABLE)
        self.assertTrue(third.triggered)
        self.assertEqual(third.observations, 3)
        self.assertAlmostEqual(third.stable_for, 0.8)

    def test_elapsed_time_alone_is_not_enough(self) -> None:
        self.tracker.update(observation(0.0))
        result = self.tracker.update(observation(1.0))
        self.assertEqual(result.state, PaperState.TRACKING)
        self.assertFalse(result.triggered)
        self.assertEqual(result.observations, 2)

    def test_movement_restarts_stability_timer(self) -> None:
        self.tracker.update(observation(0.0))
        self.tracker.update(observation(0.3))
        moved = ((180, 100), (980, 100), (980, 900), (180, 900))
        result = self.tracker.update(observation(0.6, moved))

        self.assertEqual(result.reason, "paper_moved")
        self.assertFalse(result.triggered)
        self.assertEqual(result.observations, 1)
        self.assertAlmostEqual(result.stable_for, 0.0)

        self.tracker.update(observation(1.0, moved))
        triggered = self.tracker.update(observation(1.4, moved))
        self.assertTrue(triggered.triggered)

    def test_temporary_loss_before_trigger_resets_tracking(self) -> None:
        self.tracker.update(observation(0.0))
        result = self.tracker.update(None, timestamp=0.3)
        self.assertEqual(result.state, PaperState.ABSENT)
        self.assertEqual(result.observations, 0)

        reacquired = self.tracker.update(observation(0.4))
        self.assertEqual(reacquired.reason, "paper_acquired")
        self.assertEqual(reacquired.observations, 1)

    def test_trigger_is_latched_until_paper_is_removed(self) -> None:
        self.tracker.update(observation(0.0))
        self.tracker.update(observation(0.4))
        self.assertTrue(self.tracker.update(observation(0.8)).triggered)

        held = self.tracker.update(observation(1.1))
        self.assertEqual(held.state, PaperState.LOCKED)
        self.assertFalse(held.triggered)

        pending = self.tracker.update(None, timestamp=1.2)
        removed = self.tracker.update(None, timestamp=1.7)
        self.assertEqual(pending.state, PaperState.LOCKED)
        self.assertEqual(removed.state, PaperState.ABSENT)
        self.assertEqual(removed.reason, "paper_removed")

        self.tracker.update(observation(1.8))
        self.tracker.update(observation(2.2))
        next_paper = self.tracker.update(observation(2.6))
        self.assertTrue(next_paper.triggered)
        self.assertEqual(next_paper.state, PaperState.STABLE)

    def test_geometry_is_resolution_independent(self) -> None:
        self.tracker.update(observation(0.0))
        scaled = ((200, 200), (1800, 200), (1800, 1800), (200, 1800))
        result = self.tracker.update(observation(0.4, scaled, width=2000, height=2000))
        self.assertAlmostEqual(result.iou or 0.0, 1.0)
        self.assertAlmostEqual(result.center_shift_ratio or 0.0, 0.0)
        self.assertAlmostEqual(result.area_change_ratio or 0.0, 0.0)

    def test_out_of_order_timestamp_is_rejected(self) -> None:
        self.tracker.update(observation(1.0))
        with self.assertRaises(ValueError):
            self.tracker.update(observation(0.9))

    def test_bbox_iou(self) -> None:
        self.assertEqual(bbox_iou((0, 0, 1, 1), (0, 0, 1, 1)), 1.0)
        self.assertEqual(bbox_iou((0, 0, 0.5, 0.5), (0.5, 0.5, 1, 1)), 0.0)


class PaperObservationTests(unittest.TestCase):
    def test_requires_four_finite_corners(self) -> None:
        with self.assertRaises(ValueError):
            PaperObservation.from_corners(0.0, ((0, 0), (1, 0), (1, 1)), 100, 100)
        with self.assertRaises(ValueError):
            PaperObservation.from_corners(
                0.0,
                ((0, 0), (1, 0), (float("nan"), 1), (0, 1)),
                100,
                100,
            )


if __name__ == "__main__":
    unittest.main()
