from __future__ import annotations

import unittest

import cv2
import numpy as np

from rk3588_report_parser.frame_quality import (
    BurstQualitySelector,
    DocumentFrameQualityScorer,
)
from rk3588_report_parser.paper_detector import PaperDetection


def document_image() -> np.ndarray:
    image = np.full((600, 800, 3), 220, dtype=np.uint8)
    for y in range(80, 540, 35):
        cv2.line(image, (80, y), (720, y), (20, 20, 20), 3, cv2.LINE_AA)
    cv2.putText(image, "P2540558", (180, 310), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (0, 0, 0), 5, cv2.LINE_AA)
    return image


def detection(corners=None) -> PaperDetection:
    return PaperDetection(
        corners=corners or ((40.0, 40.0), (760.0, 40.0), (760.0, 560.0), (40.0, 560.0)),
        frame_width=800,
        frame_height=600,
        confidence=0.95,
        inference_ms=3.0,
    )


def jpeg(image: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not ok:
        raise RuntimeError("test JPEG encoding failed")
    return encoded.tobytes()


class DocumentFrameQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scorer = DocumentFrameQualityScorer(cv2_module=cv2, numpy_module=np)

    def test_sharp_frame_scores_above_motion_blurred_frame(self) -> None:
        sharp = document_image()
        blurred = cv2.GaussianBlur(sharp, (31, 31), 8)
        sharp_quality = self.scorer.score_image(sharp, detection())
        blurred_quality = self.scorer.score_image(blurred, detection())

        self.assertGreater(sharp_quality.sharpness, blurred_quality.sharpness)
        self.assertGreater(sharp_quality.high_frequency_ratio, blurred_quality.high_frequency_ratio)
        self.assertLess(sharp_quality.motion_blur_risk, blurred_quality.motion_blur_risk)

    def test_specular_patch_increases_glare_ratio(self) -> None:
        normal = document_image()
        glare = normal.copy()
        cv2.rectangle(glare, (220, 140), (580, 430), (255, 255, 255), -1)
        normal_quality = self.scorer.score_image(normal, detection())
        glare_quality = self.scorer.score_image(glare, detection())
        self.assertGreater(glare_quality.glare_ratio, normal_quality.glare_ratio + 0.1)

    def test_two_frame_selector_chooses_sharp_frame(self) -> None:
        sharp = document_image()
        blurred = cv2.GaussianBlur(sharp, (31, 31), 8)
        selector = BurstQualitySelector(detection(), scorer=self.scorer)

        self.assertIsNone(selector.add_frame(jpeg(blurred), detection(), 0.0))
        selection = selector.add_frame(jpeg(sharp), detection(), 0.2)

        self.assertIsNotNone(selection)
        self.assertTrue(selection.accepted)
        self.assertEqual(selection.best_frame.index, 1)
        self.assertEqual(len(selection.frames), 2)

    def test_five_frame_selector_waits_for_the_complete_burst(self) -> None:
        image = jpeg(document_image())
        selector = BurstQualitySelector(
            detection(),
            target_frames=5,
            scorer=self.scorer,
        )

        for index in range(4):
            self.assertIsNone(selector.add_frame(image, detection(), index * 0.2))
        selection = selector.add_frame(image, detection(), 0.8)

        self.assertIsNotNone(selection)
        self.assertEqual(len(selection.frames), 5)
        self.assertTrue(selection.accepted)

    def test_different_paper_geometry_is_not_added(self) -> None:
        shifted = ((180.0, 40.0), (800.0, 40.0), (800.0, 560.0), (180.0, 560.0))
        selector = BurstQualitySelector(detection(), scorer=self.scorer)
        self.assertIsNone(selector.add_frame(jpeg(document_image()), detection(shifted), 0.0))
        self.assertEqual(len(selector.frames), 0)
        self.assertEqual(selector.rejected_frames, 1)
        self.assertEqual(selector.geometry_resets, 1)

    def test_geometry_change_reanchors_and_collects_a_fresh_burst(self) -> None:
        shifted = ((180.0, 40.0), (800.0, 40.0), (800.0, 560.0), (180.0, 560.0))
        shifted_detection = detection(shifted)
        image = jpeg(document_image())
        selector = BurstQualitySelector(detection(), target_frames=3, scorer=self.scorer)

        self.assertIsNone(selector.add_frame(image, detection(), 0.0))
        self.assertEqual(len(selector.frames), 1)
        self.assertIsNone(selector.add_frame(image, shifted_detection, 0.2))
        self.assertEqual(len(selector.frames), 0)

        self.assertIsNone(selector.add_frame(image, shifted_detection, 0.4))
        self.assertIsNone(selector.add_frame(image, shifted_detection, 0.6))
        selection = selector.add_frame(image, shifted_detection, 0.8)

        self.assertIsNotNone(selection)
        self.assertTrue(selection.accepted)
        self.assertEqual(len(selection.frames), 3)
        self.assertEqual(selector.geometry_resets, 1)

    def test_invalid_jpeg_is_skipped_without_ending_the_burst(self) -> None:
        selector = BurstQualitySelector(detection(), scorer=self.scorer)

        self.assertIsNone(selector.add_frame(b"not-a-jpeg", detection(), 0.0))

        self.assertEqual(len(selector.frames), 0)
        self.assertEqual(selector.quality_failures, 1)
        self.assertIn("decode", selector.public_status()["last_quality_error"])

    def test_temporary_missing_detection_does_not_replace_reference(self) -> None:
        selector = BurstQualitySelector(detection(), target_frames=3, scorer=self.scorer)
        image = jpeg(document_image())
        missing = PaperDetection(
            corners=(),
            frame_width=800,
            frame_height=600,
            confidence=0.1,
            inference_ms=3.0,
        )

        self.assertIsNone(selector.add_frame(image, missing, 0.0))
        self.assertIsNone(selector.add_frame(image, detection(), 0.2))
        self.assertIsNone(selector.add_frame(image, detection(), 0.4))
        selection = selector.add_frame(image, detection(), 0.6)

        self.assertIsNotNone(selection)
        self.assertTrue(selection.accepted)
        self.assertEqual(selector.geometry_resets, 0)


if __name__ == "__main__":
    unittest.main()
