"""Lightweight DocAligner ONNX inference through OpenCV DNN."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

from .paper_trigger import Point


@dataclass(frozen=True)
class PaperDetection:
    corners: Tuple[Point, ...]
    frame_width: int
    frame_height: int
    confidence: float
    inference_ms: float

    @property
    def detected(self) -> bool:
        return len(self.corners) == 4


class DocAlignerOpenCvDetector:
    """Run DocAligner's point-regression model without its Python toolkit."""

    def __init__(
        self,
        model_path: Path,
        object_threshold: float = 0.5,
        cv2_module: Optional[Any] = None,
        numpy_module: Optional[Any] = None,
        network: Optional[Any] = None,
    ) -> None:
        self.model_path = Path(model_path)
        if not self.model_path.is_file():
            raise FileNotFoundError("DocAligner ONNX model not found: %s" % self.model_path)
        if not 0 < object_threshold < 1:
            raise ValueError("object_threshold must be in the range (0, 1)")

        if cv2_module is None:
            import cv2 as cv2_module
        if numpy_module is None:
            import numpy as numpy_module

        self.cv2 = cv2_module
        self.np = numpy_module
        self.object_threshold = float(object_threshold)
        self.backend_name = "opencv_dnn_cpu"
        started = time.perf_counter()
        self.network = network or self.cv2.dnn.readNetFromONNX(str(self.model_path))
        self.model_load_ms = (time.perf_counter() - started) * 1000

    def detect_jpeg(self, image_bytes: bytes) -> PaperDetection:
        if not image_bytes:
            raise ValueError("image bytes must not be empty")
        encoded = self.np.frombuffer(image_bytes, dtype=self.np.uint8)
        image = self.cv2.imdecode(encoded, self.cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("cannot decode JPEG image")
        return self.detect_image(image)

    def detect_image(self, image: Any) -> PaperDetection:
        if image is None or len(getattr(image, "shape", ())) != 3 or image.shape[2] != 3:
            raise ValueError("DocAligner input must be a three-channel image")
        frame_height, frame_width = image.shape[:2]
        if frame_width <= 0 or frame_height <= 0:
            raise ValueError("image dimensions must be greater than zero")

        resized = self.cv2.resize(image, (256, 256))
        blob = self.np.transpose(resized, (2, 0, 1)).astype(self.np.float32)[None] / 255.0
        started = time.perf_counter()
        self.network.setInput(blob)
        point_output, object_output = self.network.forward(["points", "has_obj"])
        inference_ms = (time.perf_counter() - started) * 1000
        return _postprocess_detection(
            point_output,
            object_output,
            frame_width,
            frame_height,
            self.object_threshold,
            inference_ms,
            self.np,
        )


class DocAlignerOnnxRuntimeDetector:
    """Run the same model with ONNX Runtime when OpenCV DNN is too old."""

    def __init__(
        self,
        model_path: Path,
        object_threshold: float = 0.5,
        intra_op_threads: int = 1,
        cv2_module: Optional[Any] = None,
        numpy_module: Optional[Any] = None,
        session: Optional[Any] = None,
    ) -> None:
        self.model_path = Path(model_path)
        if not self.model_path.is_file():
            raise FileNotFoundError("DocAligner ONNX model not found: %s" % self.model_path)
        if not 0 < object_threshold < 1:
            raise ValueError("object_threshold must be in the range (0, 1)")
        if intra_op_threads < 1:
            raise ValueError("intra_op_threads must be at least one")
        if cv2_module is None:
            import cv2 as cv2_module
        if numpy_module is None:
            import numpy as numpy_module

        self.cv2 = cv2_module
        self.np = numpy_module
        self.object_threshold = float(object_threshold)
        self.intra_op_threads = int(intra_op_threads)
        self.backend_name = "onnxruntime_cpu"
        started = time.perf_counter()
        if session is None:
            import onnxruntime as ort

            options = ort.SessionOptions()
            options.intra_op_num_threads = self.intra_op_threads
            options.inter_op_num_threads = 1
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            session = ort.InferenceSession(
                str(self.model_path),
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
        self.session = session
        self.model_load_ms = (time.perf_counter() - started) * 1000

    def detect_jpeg(self, image_bytes: bytes) -> PaperDetection:
        if not image_bytes:
            raise ValueError("image bytes must not be empty")
        encoded = self.np.frombuffer(image_bytes, dtype=self.np.uint8)
        image = self.cv2.imdecode(encoded, self.cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("cannot decode JPEG image")
        return self.detect_image(image)

    def detect_image(self, image: Any) -> PaperDetection:
        if image is None or len(getattr(image, "shape", ())) != 3 or image.shape[2] != 3:
            raise ValueError("DocAligner input must be a three-channel image")
        frame_height, frame_width = image.shape[:2]
        if frame_width <= 0 or frame_height <= 0:
            raise ValueError("image dimensions must be greater than zero")

        resized = self.cv2.resize(image, (256, 256))
        blob = self.np.transpose(resized, (2, 0, 1)).astype(self.np.float32)[None] / 255.0
        started = time.perf_counter()
        point_output, object_output = self.session.run(
            ["points", "has_obj"],
            {"img": blob},
        )
        inference_ms = (time.perf_counter() - started) * 1000
        return _postprocess_detection(
            point_output,
            object_output,
            frame_width,
            frame_height,
            self.object_threshold,
            inference_ms,
            self.np,
        )


def _postprocess_detection(
    point_output: Any,
    object_output: Any,
    frame_width: int,
    frame_height: int,
    object_threshold: float,
    inference_ms: float,
    np: Any,
) -> PaperDetection:
    confidence = float(np.asarray(object_output).reshape(-1)[0])
    corners: Tuple[Point, ...] = ()
    if confidence > object_threshold:
        normalized = np.asarray(point_output, dtype=np.float32).reshape(4, 2)
        scaled = normalized * np.asarray([frame_width, frame_height], dtype=np.float32)
        corners = tuple(
            (
                max(0.0, min(float(frame_width), float(point[0]))),
                max(0.0, min(float(frame_height), float(point[1]))),
            )
            for point in scaled
        )
    return PaperDetection(
        corners=corners,
        frame_width=int(frame_width),
        frame_height=int(frame_height),
        confidence=confidence,
        inference_ms=inference_ms,
    )


def create_docaligner_detector(
    model_path: Path,
    object_threshold: float = 0.5,
    backend: str = "auto",
) -> Any:
    factories = {
        "opencv": lambda: DocAlignerOpenCvDetector(model_path, object_threshold),
        "onnxruntime": lambda: DocAlignerOnnxRuntimeDetector(model_path, object_threshold),
    }
    if backend in factories:
        return factories[backend]()
    if backend != "auto":
        raise ValueError("backend must be auto, opencv, or onnxruntime")

    errors = []
    for name in ("opencv", "onnxruntime"):
        try:
            return factories[name]()
        except Exception as exc:
            errors.append("%s: %s" % (name, exc))
    raise RuntimeError("DocAligner model could not be loaded (%s)" % "; ".join(errors))
