from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import OcrSpan


def _number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) else None


def _box_from_item(item: Dict[str, Any]) -> Optional[Tuple[int, int, int, int]]:
    box = item.get("box")
    if isinstance(box, list) and len(box) >= 4:
        values = [_number(value) for value in box[:4]]
        if all(value is not None for value in values):
            left, top, right, bottom = values  # type: ignore[misc]
            return (int(min(left, right)), int(min(top, bottom)), int(max(left, right)), int(max(top, bottom)))

    polygon = item.get("polygon")
    if isinstance(polygon, list):
        points = []
        for point in polygon:
            if not isinstance(point, list) or len(point) < 2:
                continue
            x, y = _number(point[0]), _number(point[1])
            if x is not None and y is not None:
                points.append((x, y))
        if points:
            return (
                int(min(point[0] for point in points)),
                int(min(point[1] for point in points)),
                int(max(point[0] for point in points)),
                int(max(point[1] for point in points)),
            )
    return None


def _polygon_from_item(
    item: Dict[str, Any],
    box: Tuple[int, int, int, int],
) -> Tuple[Tuple[int, int], ...]:
    raw = item.get("polygon")
    points = []
    if isinstance(raw, list):
        for point in raw:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            x, y = _number(point[0]), _number(point[1])
            if x is not None and y is not None:
                points.append((int(round(x)), int(round(y))))
    if len(points) >= 4:
        return tuple(points)
    left, top, right, bottom = box
    return ((left, top), (right, top), (right, bottom), (left, bottom))


def _normalized_box(box: Tuple[int, int, int, int], image_size: Tuple[int, int]) -> Tuple[int, int, int, int]:
    width, height = max(1, image_size[0]), max(1, image_size[1])
    left, top, right, bottom = box
    return (
        max(0, min(1000, round(left * 1000 / width))),
        max(0, min(1000, round(top * 1000 / height))),
        max(0, min(1000, round(right * 1000 / width))),
        max(0, min(1000, round(bottom * 1000 / height))),
    )


def _normalized_polygon(
    polygon: Sequence[Tuple[int, int]],
    image_size: Tuple[int, int],
) -> Tuple[Tuple[int, int], ...]:
    width, height = max(1, image_size[0]), max(1, image_size[1])
    return tuple(
        (
            max(0, min(1000, round(x * 1000 / width))),
            max(0, min(1000, round(y * 1000 / height))),
        )
        for x, y in polygon
    )


def _center_y(box: Tuple[int, int, int, int]) -> float:
    return (box[1] + box[3]) / 2.0


def _height(box: Tuple[int, int, int, int]) -> int:
    return max(1, box[3] - box[1])


def _vertical_overlap(first: Tuple[int, int, int, int], second: Tuple[int, int, int, int]) -> float:
    overlap = max(0, min(first[3], second[3]) - max(first[1], second[1]))
    return overlap / max(1, min(_height(first), _height(second)))


def _safe_alternatives(value: Any) -> Tuple[Dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    output = []
    for raw in value[:8]:
        if not isinstance(raw, dict):
            continue
        text = " ".join(str(raw.get("text") or "").split())
        if not text:
            continue
        score = raw.get("score", 0.0)
        output.append(
            {
                "text": text,
                "score": round(max(0.0, min(1.0, float(score) if isinstance(score, (int, float)) else 0.0)), 4),
                "recognition_source": str(raw.get("recognition_source") or "refinement")[:32],
            }
        )
    return tuple(output)


def build_spans(ocr_response: Dict[str, Any], image_size: Tuple[int, int]) -> List[OcrSpan]:
    """Preserve atomic OCR snippets while adding stable reading-order line IDs."""

    candidates = []
    for source_index, item in enumerate(ocr_response.get("ocr", [])):
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("text", "") or "").split())
        box = _box_from_item(item)
        if not text or box is None:
            continue
        score_raw = item.get("score", 0.0)
        score = float(score_raw) if isinstance(score_raw, (int, float)) else 0.0
        polygon = _polygon_from_item(item, box)
        candidates.append(
            {
                "source_index": source_index,
                "text": text,
                "box": box,
                "polygon": polygon,
                "score": max(0.0, min(1.0, score)),
                "recognition_source": str(item.get("recognition_source") or "primary")[:32],
                "alternatives": _safe_alternatives(item.get("alternatives")),
            }
        )

    candidates.sort(key=lambda item: (_center_y(item["box"]), item["box"][0], item["source_index"]))
    if not candidates:
        return []

    median_height = statistics.median(_height(item["box"]) for item in candidates)
    lines: List[Dict[str, Any]] = []
    for item in candidates:
        box = item["box"]
        center_y = _center_y(box)
        best_line = None
        best_distance = None
        for line in lines:
            line_box = line["box"]
            distance = abs(center_y - line["center_y"])
            tolerance = max(6.0, min(_height(box), line["median_height"], median_height) * 0.55)
            if _vertical_overlap(box, line_box) < 0.45 and distance > tolerance:
                continue
            if best_distance is None or distance < best_distance:
                best_line = line
                best_distance = distance
        if best_line is None:
            lines.append(
                {
                    "items": [item],
                    "box": box,
                    "center_y": center_y,
                    "median_height": float(_height(box)),
                }
            )
            continue
        best_line["items"].append(item)
        line_items = best_line["items"]
        best_line["box"] = (
            min(value["box"][0] for value in line_items),
            min(value["box"][1] for value in line_items),
            max(value["box"][2] for value in line_items),
            max(value["box"][3] for value in line_items),
        )
        best_line["center_y"] = statistics.median(_center_y(value["box"]) for value in line_items)
        best_line["median_height"] = statistics.median(_height(value["box"]) for value in line_items)

    lines.sort(key=lambda line: (line["center_y"], line["box"][0]))
    spans: List[OcrSpan] = []
    span_id = 0
    for line_id, line in enumerate(lines, start=1):
        for item in sorted(line["items"], key=lambda value: (value["box"][0], value["source_index"])):
            span_id += 1
            box = item["box"]
            polygon = item["polygon"]
            spans.append(
                OcrSpan(
                    id=span_id,
                    source_index=item["source_index"],
                    line_id=line_id,
                    text=item["text"],
                    box=box,
                    normalized_box=_normalized_box(box, image_size),
                    score=item["score"],
                    polygon=polygon,
                    normalized_polygon=_normalized_polygon(polygon, image_size),
                    recognition_source=item["recognition_source"],
                    alternatives=item["alternatives"],
                )
            )
    return spans


def spans_to_dicts(spans: Sequence[OcrSpan]) -> List[Dict[str, Any]]:
    return [span.to_dict() for span in spans]
