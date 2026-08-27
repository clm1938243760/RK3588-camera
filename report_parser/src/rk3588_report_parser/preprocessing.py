from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

from PIL import Image


@dataclass(frozen=True)
class PreprocessSettings:
    perspective_correction: bool = True
    min_document_area_ratio: float = 0.25
    min_confidence: float = 0.82
    min_output_side: int = 320


@dataclass(frozen=True)
class PreprocessResult:
    image: Image.Image
    applied: bool
    confidence: float
    inverse_transform: Optional[Tuple[float, ...]] = None


def _order_points(points):
    import numpy as np

    ordered = np.zeros((4, 2), dtype="float32")
    sums = points.sum(axis=1)
    differences = np.diff(points, axis=1).reshape(-1)
    ordered[0] = points[sums.argmin()]
    ordered[2] = points[sums.argmax()]
    ordered[1] = points[differences.argmin()]
    ordered[3] = points[differences.argmax()]
    return ordered


def _corner_cosine(points) -> float:
    maximum = 0.0
    for index in range(4):
        before = points[(index - 1) % 4] - points[index]
        after = points[(index + 1) % 4] - points[index]
        denominator = math.sqrt(float(before.dot(before) * after.dot(after)))
        if denominator <= 1e-6:
            return 1.0
        maximum = max(maximum, abs(float(before.dot(after))) / denominator)
    return maximum


def prepare_for_ocr(
    image: Image.Image,
    settings: Optional[PreprocessSettings] = None,
) -> PreprocessResult:
    config = settings or PreprocessSettings()
    if not config.perspective_correction:
        return PreprocessResult(image, False, 0.0)
    try:
        import cv2
        import numpy as np
    except ImportError:
        return PreprocessResult(image, False, 0.0)

    rgb = np.asarray(image.convert("RGB"))
    height, width = rgb.shape[:2]
    longest = max(width, height)
    scale = min(1.0, 1400.0 / max(longest, 1))
    if scale < 1.0:
        working = cv2.resize(rgb, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA)
    else:
        working = rgb
    gray = cv2.cvtColor(working, cv2.COLOR_RGB2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 50, 150)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8), iterations=2)
    contours = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)[0]
    image_area = float(working.shape[0] * working.shape[1])

    best = None
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:20]:
        perimeter = cv2.arcLength(contour, True)
        polygon = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        if len(polygon) != 4 or not cv2.isContourConvex(polygon):
            continue
        area = float(cv2.contourArea(polygon))
        area_ratio = area / max(image_area, 1.0)
        if not config.min_document_area_ratio <= area_ratio <= 0.97:
            continue
        points = _order_points(polygon.reshape(4, 2).astype("float32"))
        minimum_rectangle = cv2.minAreaRect(points)
        rectangle_area = float(minimum_rectangle[1][0] * minimum_rectangle[1][1])
        rectangularity = area / max(rectangle_area, 1.0)
        corner_cosine = _corner_cosine(points)
        if rectangularity < 0.78 or corner_cosine > 0.45:
            continue
        confidence = (
            0.45 * min(1.0, rectangularity)
            + 0.35 * max(0.0, 1.0 - corner_cosine / 0.45)
            + 0.20 * min(1.0, area_ratio / 0.70)
        )
        if best is None or confidence > best[0]:
            best = (confidence, points / scale)

    if best is None or best[0] < config.min_confidence:
        return PreprocessResult(image, False, float(best[0]) if best else 0.0)

    confidence, source = best
    top_width = float(np.linalg.norm(source[1] - source[0]))
    bottom_width = float(np.linalg.norm(source[2] - source[3]))
    left_height = float(np.linalg.norm(source[3] - source[0]))
    right_height = float(np.linalg.norm(source[2] - source[1]))
    output_width = int(round(max(top_width, bottom_width)))
    output_height = int(round(max(left_height, right_height)))
    if min(output_width, output_height) < config.min_output_side:
        return PreprocessResult(image, False, confidence)

    destination = np.array(
        [[0, 0], [output_width - 1, 0], [output_width - 1, output_height - 1], [0, output_height - 1]],
        dtype="float32",
    )
    transform = cv2.getPerspectiveTransform(source.astype("float32"), destination)
    inverse = cv2.getPerspectiveTransform(destination, source.astype("float32"))
    warped = cv2.warpPerspective(rgb, transform, (output_width, output_height), flags=cv2.INTER_CUBIC)
    return PreprocessResult(
        Image.fromarray(warped, mode="RGB"),
        True,
        confidence,
        tuple(float(value) for value in inverse.reshape(-1)),
    )


def _map_point(matrix: Sequence[float], x: float, y: float) -> Tuple[float, float]:
    denominator = matrix[6] * x + matrix[7] * y + matrix[8]
    if abs(denominator) < 1e-9:
        return x, y
    return (
        (matrix[0] * x + matrix[1] * y + matrix[2]) / denominator,
        (matrix[3] * x + matrix[4] * y + matrix[5]) / denominator,
    )


def restore_ocr_coordinates(
    response: Dict[str, Any],
    inverse_transform: Optional[Sequence[float]],
    original_size: Tuple[int, int],
) -> Dict[str, Any]:
    if inverse_transform is None or not isinstance(response.get("ocr"), list):
        return response
    width, height = original_size
    restored = dict(response)
    items = []
    for raw_item in response["ocr"]:
        if not isinstance(raw_item, dict):
            items.append(raw_item)
            continue
        item = dict(raw_item)
        polygon = item.get("polygon")
        if not isinstance(polygon, list) or len(polygon) < 4:
            box = item.get("box")
            if not isinstance(box, (list, tuple)) or len(box) != 4:
                items.append(item)
                continue
            left, top, right, bottom = (float(value) for value in box)
            polygon = [[left, top], [right, top], [right, bottom], [left, bottom]]
        points = []
        for point in polygon:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                points = []
                break
            mapped_x, mapped_y = _map_point(inverse_transform, float(point[0]), float(point[1]))
            points.append([max(0.0, min(float(width), mapped_x)), max(0.0, min(float(height), mapped_y))])
        if not points:
            items.append(item)
            continue
        item["polygon"] = [[round(x, 2), round(y, 2)] for x, y in points]
        item["box"] = [
            round(min(point[0] for point in points), 2),
            round(min(point[1] for point in points), 2),
            round(max(point[0] for point in points), 2),
            round(max(point[1] for point in points), 2),
        ]
        items.append(item)
    restored["ocr"] = items
    return restored
