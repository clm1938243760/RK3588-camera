"""Rotate camera JPEGs for OCR without changing the capture stream."""

from __future__ import annotations

import io
import math
from typing import Any

from PIL import Image

from .paper_detector import PaperDetection


VALID_ROTATIONS = (0, 90, 180, 270)


def rotate_jpeg(image_bytes: bytes, degrees_counterclockwise: int) -> bytes:
    if degrees_counterclockwise not in VALID_ROTATIONS:
        raise ValueError("JPEG rotation must be one of 0, 90, 180, or 270 degrees")
    if degrees_counterclockwise == 0:
        return image_bytes

    transpose_namespace = getattr(Image, "Transpose", Image)
    transpose = {
        90: transpose_namespace.ROTATE_90,
        180: transpose_namespace.ROTATE_180,
        270: transpose_namespace.ROTATE_270,
    }[degrees_counterclockwise]
    with Image.open(io.BytesIO(image_bytes)) as source:
        rotated = source.convert("RGB").transpose(transpose)
        output = io.BytesIO()
        rotated.save(output, format="JPEG", quality=95, subsampling=0)
    return output.getvalue()


def prepare_document_jpeg(
    image_bytes: bytes,
    detection: PaperDetection,
    degrees_counterclockwise: int = 0,
    target_long_side: int = 3200,
    cv2_module: Any = None,
    numpy_module: Any = None,
) -> bytes:
    if not detection.detected:
        raise ValueError("document preparation requires four detected corners")
    if target_long_side < 640:
        raise ValueError("target document long side must be at least 640 pixels")
    if degrees_counterclockwise not in VALID_ROTATIONS:
        raise ValueError("JPEG rotation must be one of 0, 90, 180, or 270 degrees")
    if cv2_module is None:
        import cv2 as cv2_module
    if numpy_module is None:
        import numpy as numpy_module

    encoded = numpy_module.frombuffer(image_bytes, dtype=numpy_module.uint8)
    image = cv2_module.imdecode(encoded, cv2_module.IMREAD_COLOR)
    if image is None:
        raise ValueError("cannot decode camera JPEG")

    points = numpy_module.asarray(detection.corners, dtype=numpy_module.float32)
    width = max(
        _distance(points[0], points[1]),
        _distance(points[3], points[2]),
    )
    height = max(
        _distance(points[0], points[3]),
        _distance(points[1], points[2]),
    )
    output_width = max(32, int(round(width)))
    output_height = max(32, int(round(height)))
    destination = numpy_module.asarray(
        [
            [0, 0],
            [output_width - 1, 0],
            [output_width - 1, output_height - 1],
            [0, output_height - 1],
        ],
        dtype=numpy_module.float32,
    )
    transform = cv2_module.getPerspectiveTransform(points, destination)
    document = cv2_module.warpPerspective(
        image,
        transform,
        (output_width, output_height),
        flags=cv2_module.INTER_CUBIC,
        borderMode=cv2_module.BORDER_REPLICATE,
    )
    rotation_codes = {
        90: cv2_module.ROTATE_90_COUNTERCLOCKWISE,
        180: cv2_module.ROTATE_180,
        270: cv2_module.ROTATE_90_CLOCKWISE,
    }
    if degrees_counterclockwise:
        document = cv2_module.rotate(document, rotation_codes[degrees_counterclockwise])

    current_long_side = max(document.shape[:2])
    scale = target_long_side / max(1, current_long_side)
    if scale > 1.0:
        scale = min(2.5, scale)
    if scale > 1.01:
        document = cv2_module.resize(
            document,
            None,
            fx=scale,
            fy=scale,
            interpolation=cv2_module.INTER_CUBIC,
        )
    elif scale < 0.99:
        document = cv2_module.resize(
            document,
            None,
            fx=scale,
            fy=scale,
            interpolation=cv2_module.INTER_AREA,
        )
    ok, prepared = cv2_module.imencode(
        ".jpg",
        document,
        [cv2_module.IMWRITE_JPEG_QUALITY, 95],
    )
    if not ok:
        raise ValueError("cannot encode prepared document JPEG")
    return prepared.tobytes()


def _distance(first: Any, second: Any) -> float:
    return math.hypot(float(first[0] - second[0]), float(first[1] - second[1]))
