from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from rk3588_report_parser.paper_detector import (
    DocAlignerOnnxRuntimeDetector,
    DocAlignerOpenCvDetector,
)


class FakeNetwork:
    def __init__(self, confidence: float) -> None:
        self.confidence = confidence
        self.input = None

    def setInput(self, value) -> None:
        self.input = value

    def forward(self, names):
        if list(names) != ["points", "has_obj"]:
            raise AssertionError(names)
        points = np.asarray([[0.1, 0.2, 0.9, 0.2, 0.9, 0.8, 0.1, 0.8]], dtype=np.float32)
        has_object = np.asarray([[self.confidence]], dtype=np.float32)
        return points, has_object


class FakeSession:
    def __init__(self, confidence: float) -> None:
        self.confidence = confidence
        self.inputs = None

    def run(self, names, inputs):
        if list(names) != ["points", "has_obj"]:
            raise AssertionError(names)
        self.inputs = inputs
        points = np.asarray([[0.1, 0.2, 0.9, 0.2, 0.9, 0.8, 0.1, 0.8]], dtype=np.float32)
        has_object = np.asarray([[self.confidence]], dtype=np.float32)
        return points, has_object


class FakeCv2:
    IMREAD_COLOR = 1

    @staticmethod
    def imdecode(encoded, mode):
        del encoded, mode
        return np.zeros((100, 200, 3), dtype=np.uint8)

    @staticmethod
    def resize(image, size):
        del image
        return np.zeros((size[1], size[0], 3), dtype=np.uint8)


class DocAlignerOpenCvDetectorTests(unittest.TestCase):
    def make_detector(self, directory: str, confidence: float) -> DocAlignerOpenCvDetector:
        model = Path(directory) / "model.onnx"
        model.write_bytes(b"test model placeholder")
        return DocAlignerOpenCvDetector(
            model,
            cv2_module=FakeCv2,
            numpy_module=np,
            network=FakeNetwork(confidence),
        )

    def test_scales_model_points_to_original_frame(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            detector = self.make_detector(directory, 0.9)
            result = detector.detect_jpeg(b"jpeg")

        self.assertTrue(result.detected)
        self.assertAlmostEqual(result.confidence, 0.9, places=5)
        self.assertEqual(result.frame_width, 200)
        self.assertEqual(result.frame_height, 100)
        expected = ((20.0, 20.0), (180.0, 20.0), (180.0, 80.0), (20.0, 80.0))
        for actual_point, expected_point in zip(result.corners, expected):
            self.assertAlmostEqual(actual_point[0], expected_point[0], places=4)
            self.assertAlmostEqual(actual_point[1], expected_point[1], places=4)
        self.assertEqual(detector.network.input.shape, (1, 3, 256, 256))

    def test_low_object_confidence_returns_no_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self.make_detector(directory, 0.4).detect_jpeg(b"jpeg")
        self.assertFalse(result.detected)
        self.assertEqual(result.corners, ())

    def test_onnxruntime_backend_uses_the_same_output_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "model.onnx"
            model.write_bytes(b"test model placeholder")
            session = FakeSession(0.9)
            detector = DocAlignerOnnxRuntimeDetector(
                model,
                cv2_module=FakeCv2,
                numpy_module=np,
                session=session,
            )
            result = detector.detect_jpeg(b"jpeg")

        self.assertTrue(result.detected)
        self.assertEqual(detector.backend_name, "onnxruntime_cpu")
        self.assertEqual(detector.intra_op_threads, 1)
        self.assertEqual(session.inputs["img"].shape, (1, 3, 256, 256))
        self.assertAlmostEqual(result.corners[0][0], 20.0, places=4)
        self.assertAlmostEqual(result.corners[0][1], 20.0, places=4)

    def test_model_file_and_threshold_are_validated(self) -> None:
        with self.assertRaises(FileNotFoundError):
            DocAlignerOpenCvDetector(Path("missing.onnx"), cv2_module=FakeCv2, numpy_module=np)
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "model.onnx"
            model.write_bytes(b"x")
            with self.assertRaises(ValueError):
                DocAlignerOpenCvDetector(
                    model,
                    object_threshold=1.0,
                    cv2_module=FakeCv2,
                    numpy_module=np,
                    network=FakeNetwork(0.9),
                )
            with self.assertRaises(ValueError):
                DocAlignerOnnxRuntimeDetector(
                    model,
                    intra_op_threads=0,
                    cv2_module=FakeCv2,
                    numpy_module=np,
                    session=FakeSession(0.9),
                )


if __name__ == "__main__":
    unittest.main()
