"""Full-page OCR text retained from a verified camera capture."""

from __future__ import annotations

import io
import math
import statistics
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple

from PIL import Image, ImageOps

from .models import OcrSpan, average_score
from .settings import OcrSettings
from .spans import build_spans


class FullTextOcrClientProtocol(Protocol):
    def recognize(self, image_bytes: bytes, settings: OcrSettings) -> Dict[str, Any]:
        ...


@dataclass(frozen=True)
class CapturedTextLine:
    line_id: int
    text: str = field(repr=False)
    span_ids: Tuple[int, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "line_id": self.line_id,
            "text": self.text,
            "span_ids": list(self.span_ids),
        }


@dataclass(frozen=True)
class CapturedTextDocument:
    image_size: Tuple[int, int]
    lines: Tuple[CapturedTextLine, ...] = field(repr=False)
    spans: Tuple[OcrSpan, ...] = field(repr=False)
    full_text: str = field(repr=False)
    mean_confidence: float

    def public_status(self) -> Dict[str, Any]:
        return {
            "available": bool(self.spans),
            "line_count": len(self.lines),
            "item_count": len(self.spans),
            "mean_confidence": round(self.mean_confidence, 4),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": 2,
            "image_size": list(self.image_size),
            "full_text": self.full_text,
            "lines": [line.to_dict() for line in self.lines],
            "blocks": [span.to_dict() for span in self.spans],
            "line_count": len(self.lines),
            "item_count": len(self.spans),
            "mean_confidence": round(self.mean_confidence, 4),
        }


@dataclass(frozen=True)
class FullTextExtraction:
    status: str
    document: Optional[CapturedTextDocument] = field(default=None, repr=False)
    elapsed_ms: float = 0.0
    reasons: Tuple[str, ...] = ()
    timings: Dict[str, float] = field(default_factory=dict)
    refinement_regions: int = 0
    conflict_count: int = 0

    @property
    def accepted(self) -> bool:
        return self.status == "accepted" and self.document is not None

    @property
    def available(self) -> bool:
        return self.status in {"accepted", "review_required"} and self.document is not None

    def public_status(self) -> Dict[str, Any]:
        status = (
            self.document.public_status()
            if self.document is not None
            else {"available": False, "line_count": 0, "item_count": 0, "mean_confidence": 0.0}
        )
        return {
            **status,
            "status": self.status,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "reasons": list(self.reasons),
            "refinement_regions": self.refinement_regions,
            "conflict_count": self.conflict_count,
            "timings": {key: round(value, 2) for key, value in self.timings.items()},
        }


@dataclass(frozen=True)
class TextRefinementSettings:
    low_confidence: float = 0.70
    low_mean_confidence: float = 0.65
    conflict_score_delta: float = 0.08
    max_regions: int = 3
    max_crop_long_side: int = 1600
    max_duration_seconds: float = 10.0
    primary_tile_max_aspect: float = 0.0
    primary_tile_overlap_ratio: float = 0.15
    primary_tile_max_count: int = 4
    primary_partition_mode: str = "strips"
    primary_grid_min_ink_density: float = 0.012


@dataclass(frozen=True)
class OcrRegion:
    left: int
    top: int
    right: int
    bottom: int
    priority: float = 0.0

    @property
    def width(self) -> int:
        return max(1, self.right - self.left)

    @property
    def height(self) -> int:
        return max(1, self.bottom - self.top)


@dataclass(frozen=True)
class OcrTile:
    region: OcrRegion
    axis: str
    core_start: int
    core_end: int
    core_region: Optional[OcrRegion] = None


class FullTextExtractor:
    def __init__(
        self,
        ocr_client: FullTextOcrClientProtocol,
        settings: OcrSettings,
        refinement: Optional[TextRefinementSettings] = None,
    ) -> None:
        self.ocr_client = ocr_client
        self.settings = settings
        self.refinement = refinement or TextRefinementSettings()

    def extract(self, image_bytes: bytes) -> FullTextExtraction:
        return self.extract_refined(image_bytes)

    def extract_refined(
        self,
        primary_image_bytes: bytes,
        secondary_image_bytes: Optional[bytes] = None,
        progress: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> FullTextExtraction:
        started = time.monotonic()
        timings: Dict[str, float] = {}
        reasons: List[str] = []
        conflict_count = 0
        refinement_regions = 0
        try:
            with Image.open(io.BytesIO(primary_image_bytes)) as image:
                image_size = image.size
                primary_image = image.convert("RGB")
            tiles = _primary_tiles(image_size, self.refinement, primary_image)
            primary_started = time.monotonic()
            if tiles:
                _progress(progress, "ocr_primary", {"mode": "tiled", "tile_count": len(tiles)})
                items = []
                processed_tiles = 0
                try:
                    for index, tile in enumerate(tiles):
                        if self._deadline_exceeded(started):
                            reasons.append("time_budget_exceeded")
                            break
                        tile_bytes, pad_left, pad_top = _render_primary_tile(primary_image, tile.region)
                        response = self.ocr_client.recognize(
                            tile_bytes,
                            self._remaining_settings(started),
                        )
                        tiled_items = _map_primary_tile_items(
                            response,
                            tile,
                            pad_left,
                            pad_top,
                            "primary_tile_%d" % (index + 1),
                        )
                        items, conflicts = _merge_items(
                            items,
                            tiled_items,
                            self.refinement.conflict_score_delta,
                        )
                        conflict_count += conflicts
                        processed_tiles += 1
                finally:
                    primary_image.close()
                timings["primary_tile_count"] = float(processed_tiles)
            else:
                _progress(progress, "ocr_primary", {"mode": "full_page"})
                try:
                    response = self.ocr_client.recognize(primary_image_bytes, self.settings)
                finally:
                    primary_image.close()
                items = _response_items(response, "primary")
            timings["primary_ocr_ms"] = (time.monotonic() - primary_started) * 1000.0
            spans = build_spans({"ocr": items}, image_size)

            aligned_secondary = None
            if secondary_image_bytes is not None:
                aligned_secondary = _align_image(secondary_image_bytes, image_size)

            if not spans and aligned_secondary is not None and not self._deadline_exceeded(started):
                _progress(progress, "ocr_refining", {"mode": "secondary_full"})
                retry_started = time.monotonic()
                response = self.ocr_client.recognize(
                    aligned_secondary,
                    self._remaining_settings(started),
                )
                timings["secondary_full_ocr_ms"] = (time.monotonic() - retry_started) * 1000.0
                items = _response_items(response, "secondary_full")
                spans = build_spans({"ocr": items}, image_size)

            regions = []
            if spans and aligned_secondary is not None and not self._deadline_exceeded(started):
                regions = _candidate_regions(spans, image_size, self.refinement)
            if regions:
                _progress(progress, "ocr_refining", {"region_count": len(regions)})
                secondary_image = Image.open(io.BytesIO(aligned_secondary)).convert("RGB")
                try:
                    for region in regions:
                        if self._deadline_exceeded(started):
                            reasons.append("time_budget_exceeded")
                            break
                        crop_bytes, scale_x, scale_y = _render_crop(
                            secondary_image,
                            region,
                            self.refinement.max_crop_long_side,
                        )
                        crop_started = time.monotonic()
                        crop_response = self.ocr_client.recognize(
                            crop_bytes,
                            self._remaining_settings(started),
                        )
                        timings["refinement_ocr_ms"] = timings.get("refinement_ocr_ms", 0.0) + (
                            time.monotonic() - crop_started
                        ) * 1000.0
                        refinement_regions += 1
                        refined_items = _map_crop_items(
                            crop_response,
                            region,
                            scale_x,
                            scale_y,
                        )
                        items, conflicts = _merge_items(
                            items,
                            refined_items,
                            self.refinement.conflict_score_delta,
                        )
                        conflict_count += conflicts
                finally:
                    secondary_image.close()
                spans = build_spans({"ocr": items}, image_size)
        except Exception as exc:
            return FullTextExtraction(
                status="error",
                elapsed_ms=(time.monotonic() - started) * 1000.0,
                reasons=("full_text_error:%s" % type(exc).__name__,),
                timings=timings,
                refinement_regions=refinement_regions,
                conflict_count=conflict_count,
            )
        if not spans:
            return FullTextExtraction(
                status="rejected",
                elapsed_ms=(time.monotonic() - started) * 1000.0,
                reasons=("no_ocr_text_blocks",),
                timings=timings,
                refinement_regions=refinement_regions,
                conflict_count=conflict_count,
            )
        mean_confidence = average_score(spans)
        if conflict_count:
            reasons.append("ocr_conflict")
        if mean_confidence < self.refinement.low_mean_confidence:
            reasons.append("low_mean_confidence")
        if any(span.score < self.refinement.low_confidence for span in spans):
            reasons.append("low_confidence_blocks")
        if self._deadline_exceeded(started):
            reasons.append("time_budget_exceeded")
        reasons = list(dict.fromkeys(reasons))
        timings["total_ms"] = (time.monotonic() - started) * 1000.0
        return FullTextExtraction(
            status="review_required" if reasons else "accepted",
            document=build_captured_text_document(spans, image_size),
            elapsed_ms=timings["total_ms"],
            reasons=tuple(reasons),
            timings=timings,
            refinement_regions=refinement_regions,
            conflict_count=conflict_count,
        )

    def _deadline_exceeded(self, started: float) -> bool:
        return time.monotonic() - started >= self.refinement.max_duration_seconds

    def _remaining_settings(self, started: float) -> OcrSettings:
        remaining = max(1.0, self.refinement.max_duration_seconds - (time.monotonic() - started))
        return replace(self.settings, timeout_seconds=min(self.settings.timeout_seconds, remaining))


def _progress(
    callback: Optional[Callable[[str, Dict[str, Any]], None]],
    stage: str,
    details: Dict[str, Any],
) -> None:
    if callback is not None:
        callback(stage, details)


def _response_items(response: Dict[str, Any], source: str) -> List[Dict[str, Any]]:
    if not isinstance(response, dict) or not isinstance(response.get("ocr"), list):
        raise ValueError("OCR response is missing ocr items")
    items = []
    for raw in response["ocr"]:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        item["recognition_source"] = source
        item["alternatives"] = list(item.get("alternatives") or [])
        items.append(item)
    return items


def _align_image(image_bytes: bytes, image_size: Tuple[int, int]) -> bytes:
    with Image.open(io.BytesIO(image_bytes)) as source:
        image = source.convert("RGB")
        if image.size != image_size:
            namespace = getattr(Image, "Resampling", Image)
            image = image.resize(image_size, namespace.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=95, subsampling=0)
    return output.getvalue()


def _primary_tiles(
    image_size: Tuple[int, int],
    settings: TextRefinementSettings,
    image: Optional[Image.Image] = None,
) -> List[OcrTile]:
    width, height = image_size
    if settings.primary_partition_mode == "adaptive" and image is not None:
        grid_tiles = _adaptive_grid_tiles(image, settings)
        if grid_tiles:
            return grid_tiles
    if (
        settings.primary_tile_max_aspect <= 1.0
        or settings.primary_tile_max_count < 2
        or min(width, height) < 1
    ):
        return []
    long_side = max(width, height)
    short_side = min(width, height)
    if long_side / short_side <= settings.primary_tile_max_aspect:
        return []

    axis = "y" if height >= width else "x"
    tile_count = min(
        settings.primary_tile_max_count,
        max(2, math.ceil(long_side / (short_side * settings.primary_tile_max_aspect))),
    )
    overlap = max(
        0,
        min(
            short_side // 2,
            round(short_side * max(0.0, min(0.45, settings.primary_tile_overlap_ratio))),
        ),
    )
    extent = min(long_side, math.ceil((long_side + overlap * (tile_count - 1)) / tile_count))
    step = max(1, extent - overlap)
    starts = [min(index * step, long_side - extent) for index in range(tile_count)]
    starts[-1] = long_side - extent

    regions = []
    for start in starts:
        if axis == "y":
            regions.append(OcrRegion(0, start, width, start + extent))
        else:
            regions.append(OcrRegion(start, 0, start + extent, height))

    boundaries = [0]
    for first, second in zip(regions, regions[1:]):
        first_end = first.bottom if axis == "y" else first.right
        second_start = second.top if axis == "y" else second.left
        boundaries.append(round((first_end + second_start) / 2.0))
    boundaries.append(long_side)
    return [
        OcrTile(region, axis, boundaries[index], boundaries[index + 1])
        for index, region in enumerate(regions)
    ]


def _adaptive_grid_tiles(image: Image.Image, settings: TextRefinementSettings) -> List[OcrTile]:
    """Split a dense page at real gutters before fixed-size RKNN detection.

    This is deliberately generic image segmentation: it looks only for ink-density
    valleys, not report labels, fields, or fixed coordinates.  A grid is used only
    when both cuts have a clear central gutter.  The existing strip splitter stays
    as the safe fallback for sparse or irregular pages.
    """

    width, height = image.size
    if (
        settings.primary_tile_max_count < 4
        or min(width, height) < 640
        or settings.primary_tile_max_aspect <= 1.0
    ):
        return []
    horizontal, vertical, ink_density = _ink_profiles(image)
    if ink_density < settings.primary_grid_min_ink_density:
        return []
    x_cut = _central_gutter(vertical)
    y_cut = _central_gutter(horizontal)
    if x_cut is None or y_cut is None:
        return []
    scaled_width = max(1, len(vertical))
    scaled_height = max(1, len(horizontal))
    split_x = max(1, min(width - 1, round(x_cut * width / scaled_width)))
    split_y = max(1, min(height - 1, round(y_cut * height / scaled_height)))
    if min(split_x, width - split_x, split_y, height - split_y) < min(width, height) * 0.18:
        return []

    overlap_x = max(12, round(min(split_x, width - split_x) * settings.primary_tile_overlap_ratio))
    overlap_y = max(12, round(min(split_y, height - split_y) * settings.primary_tile_overlap_ratio))
    output = []
    for top, bottom in ((0, split_y), (split_y, height)):
        for left, right in ((0, split_x), (split_x, width)):
            core = OcrRegion(left, top, right, bottom)
            region = OcrRegion(
                max(0, left - overlap_x),
                max(0, top - overlap_y),
                min(width, right + overlap_x),
                min(height, bottom + overlap_y),
            )
            output.append(OcrTile(region, "xy", 0, 0, core_region=core))
    return output


def _ink_profiles(image: Image.Image, maximum_side: int = 640) -> Tuple[List[float], List[float], float]:
    """Return smoothed row/column ink density without a heavyweight vision model."""

    grayscale = ImageOps.autocontrast(image.convert("L"), cutoff=1)
    try:
        width, height = grayscale.size
        scale = min(1.0, maximum_side / max(width, height))
        if scale < 1.0:
            namespace = getattr(Image, "Resampling", Image)
            grayscale = grayscale.resize(
                (max(1, round(width * scale)), max(1, round(height * scale))),
                namespace.BILINEAR,
            )
        width, height = grayscale.size
        values = list(grayscale.getdata())
    finally:
        grayscale.close()
    if not values or width < 2 or height < 2:
        return [], [], 0.0
    histogram = [0] * 256
    for value in values:
        histogram[int(value)] += 1
    target = max(1, round(len(values) * 0.90))
    cumulative = 0
    background = 255
    for value, count in enumerate(histogram):
        cumulative += count
        if cumulative >= target:
            background = value
            break
    threshold = max(130, min(225, background - 28))
    rows = [0] * height
    columns = [0] * width
    ink_count = 0
    for index, value in enumerate(values):
        if value >= threshold:
            continue
        y, x = divmod(index, width)
        rows[y] += 1
        columns[x] += 1
        ink_count += 1
    return (
        _smooth_profile([value / width for value in rows]),
        _smooth_profile([value / height for value in columns]),
        ink_count / len(values),
    )


def _smooth_profile(profile: Sequence[float]) -> List[float]:
    if len(profile) < 9:
        return list(profile)
    radius = max(2, min(12, len(profile) // 80))
    output = []
    for index in range(len(profile)):
        start = max(0, index - radius)
        end = min(len(profile), index + radius + 1)
        output.append(sum(profile[start:end]) / max(1, end - start))
    return output


def _central_gutter(profile: Sequence[float]) -> Optional[int]:
    if len(profile) < 32:
        return None
    start = max(1, round(len(profile) * 0.28))
    end = min(len(profile) - 1, round(len(profile) * 0.72))
    if end <= start:
        return None
    average = sum(profile) / len(profile)
    if average <= 0.001:
        return None
    index = min(range(start, end), key=lambda value: profile[value])
    left = sum(profile[:index]) / max(1, index)
    right = sum(profile[index + 1:]) / max(1, len(profile) - index - 1)
    if left < average * 0.35 or right < average * 0.35:
        return None
    if profile[index] > max(0.006, average * 0.58):
        return None
    return index


def _render_primary_tile(image: Image.Image, region: OcrRegion) -> Tuple[bytes, int, int]:
    crop = image.crop((region.left, region.top, region.right, region.bottom))
    side = max(crop.size)
    pad_left = (side - crop.width) // 2
    pad_top = (side - crop.height) // 2
    canvas = Image.new("RGB", (side, side), "white")
    canvas.paste(crop, (pad_left, pad_top))
    output = io.BytesIO()
    canvas.save(output, format="JPEG", quality=95, subsampling=0)
    return output.getvalue(), pad_left, pad_top


def _map_primary_tile_items(
    response: Dict[str, Any],
    tile: OcrTile,
    pad_left: int,
    pad_top: int,
    source: str,
) -> List[Dict[str, Any]]:
    output = []
    for item in _response_items(response, source):
        item_box = _item_box(item)
        if item_box is None:
            continue
        left, top, right, bottom = item_box
        mapped_box = [
            max(tile.region.left, tile.region.left + left - pad_left),
            max(tile.region.top, tile.region.top + top - pad_top),
            min(tile.region.right, tile.region.left + right - pad_left),
            min(tile.region.bottom, tile.region.top + bottom - pad_top),
        ]
        if mapped_box[2] <= mapped_box[0] or mapped_box[3] <= mapped_box[1]:
            continue
        center_x = (mapped_box[0] + mapped_box[2]) / 2.0
        center_y = (mapped_box[1] + mapped_box[3]) / 2.0
        if tile.core_region is not None:
            core = tile.core_region
            if not (core.left <= center_x < core.right and core.top <= center_y < core.bottom):
                continue
        else:
            center = center_y if tile.axis == "y" else center_x
            if not tile.core_start <= center < tile.core_end:
                continue
        item["box"] = [round(value, 2) for value in mapped_box]
        polygon = item.get("polygon")
        if isinstance(polygon, list):
            mapped_polygon = []
            for point in polygon:
                if not isinstance(point, (list, tuple)) or len(point) < 2:
                    continue
                x = min(
                    tile.region.right,
                    max(tile.region.left, tile.region.left + float(point[0]) - pad_left),
                )
                y = min(
                    tile.region.bottom,
                    max(tile.region.top, tile.region.top + float(point[1]) - pad_top),
                )
                mapped_polygon.append([round(x, 2), round(y, 2)])
            item["polygon"] = mapped_polygon
        output.append(item)
    return output


def _candidate_regions(
    spans: Sequence[OcrSpan],
    image_size: Tuple[int, int],
    settings: TextRefinementSettings,
) -> List[OcrRegion]:
    if not spans or settings.max_regions < 1:
        return []
    width, height = image_size
    median_height = statistics.median(max(1, span.box[3] - span.box[1]) for span in spans)
    mean_confidence = average_score(spans)
    candidates = []
    for span in spans:
        left, top, right, bottom = span.box
        span_height = max(1, bottom - top)
        small = span_height < max(10.0, median_height * 0.55)
        touches_edge = left <= span_height or top <= span_height or right >= width - span_height or bottom >= height - span_height
        if span.score >= settings.low_confidence and not small and not touches_edge:
            continue
        margin = max(24, round(span_height * 2.0))
        candidates.append(
            OcrRegion(
                max(0, left - margin),
                max(0, top - margin),
                min(width, right + margin),
                min(height, bottom + margin),
                priority=span.score,
            )
        )
    if mean_confidence < settings.low_mean_confidence and not candidates:
        for span in sorted(spans, key=lambda value: value.score)[: settings.max_regions]:
            left, top, right, bottom = span.box
            margin = max(24, round(max(1, bottom - top) * 2.0))
            candidates.append(
                OcrRegion(
                    max(0, left - margin),
                    max(0, top - margin),
                    min(width, right + margin),
                    min(height, bottom + margin),
                    priority=span.score,
                )
            )
    return _merge_regions(candidates, settings.max_regions)


def _region_overlap(first: OcrRegion, second: OcrRegion) -> float:
    intersection = max(0, min(first.right, second.right) - max(first.left, second.left)) * max(
        0, min(first.bottom, second.bottom) - max(first.top, second.top)
    )
    return intersection / max(1, min(first.width * first.height, second.width * second.height))


def _merge_regions(regions: Sequence[OcrRegion], limit: int) -> List[OcrRegion]:
    merged: List[OcrRegion] = []
    for region in sorted(regions, key=lambda value: (value.priority, value.top, value.left)):
        for index, current in enumerate(merged):
            if _region_overlap(region, current) < 0.30:
                continue
            merged[index] = OcrRegion(
                min(region.left, current.left),
                min(region.top, current.top),
                max(region.right, current.right),
                max(region.bottom, current.bottom),
                min(region.priority, current.priority),
            )
            break
        else:
            merged.append(region)
    return merged[:limit]


def _render_crop(image: Image.Image, region: OcrRegion, max_long_side: int) -> Tuple[bytes, float, float]:
    crop = image.crop((region.left, region.top, region.right, region.bottom))
    scale = min(4.0, max_long_side / max(crop.size))
    if scale > 1.01:
        namespace = getattr(Image, "Resampling", Image)
        resized = crop.resize(
            (max(1, round(crop.width * scale)), max(1, round(crop.height * scale))),
            namespace.LANCZOS,
        )
    else:
        resized = crop
    output = io.BytesIO()
    resized.save(output, format="JPEG", quality=95, subsampling=0)
    return output.getvalue(), resized.width / crop.width, resized.height / crop.height


def _map_crop_items(
    response: Dict[str, Any],
    region: OcrRegion,
    scale_x: float,
    scale_y: float,
) -> List[Dict[str, Any]]:
    with_items = _response_items(response, "refinement")
    output = []
    for item in with_items:
        box = item.get("box")
        if not isinstance(box, list) or len(box) < 4:
            continue
        left, top, right, bottom = (float(value) for value in box[:4])
        item["box"] = [
            round(region.left + left / scale_x, 2),
            round(region.top + top / scale_y, 2),
            round(region.left + right / scale_x, 2),
            round(region.top + bottom / scale_y, 2),
        ]
        polygon = item.get("polygon")
        if isinstance(polygon, list):
            item["polygon"] = [
                [round(region.left + float(point[0]) / scale_x, 2), round(region.top + float(point[1]) / scale_y, 2)]
                for point in polygon
                if isinstance(point, (list, tuple)) and len(point) >= 2
            ]
        output.append(item)
    return output


def _item_box(item: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    box = item.get("box")
    if not isinstance(box, (list, tuple)) or len(box) < 4:
        return None
    try:
        left, top, right, bottom = (float(value) for value in box[:4])
    except (TypeError, ValueError):
        return None
    return min(left, right), min(top, bottom), max(left, right), max(top, bottom)


def _box_overlap(first: Sequence[float], second: Sequence[float]) -> float:
    intersection = max(0.0, min(first[2], second[2]) - max(first[0], second[0])) * max(
        0.0, min(first[3], second[3]) - max(first[1], second[1])
    )
    first_area = max(1.0, (first[2] - first[0]) * (first[3] - first[1]))
    second_area = max(1.0, (second[2] - second[0]) * (second[3] - second[1]))
    return intersection / min(first_area, second_area)


def _alternative(item: Dict[str, Any]) -> Dict[str, Any]:
    score = item.get("score", 0.0)
    return {
        "text": " ".join(str(item.get("text") or "").split()),
        "score": round(float(score) if isinstance(score, (int, float)) else 0.0, 4),
        "recognition_source": str(item.get("recognition_source") or "refinement")[:32],
    }


def _append_unique_alternative(
    alternatives: List[Dict[str, Any]],
    item: Dict[str, Any],
) -> bool:
    alternative = _alternative(item)
    if not alternative["text"]:
        return False
    if any(
        str(existing.get("text") or "") == alternative["text"]
        for existing in alternatives
        if isinstance(existing, dict)
    ):
        return False
    alternatives.append(alternative)
    return True


def _merged_recognition_source(first: Dict[str, Any], second: Dict[str, Any]) -> str:
    sources = {
        str(first.get("recognition_source") or ""),
        str(second.get("recognition_source") or ""),
    }
    if sources and all(source.startswith("primary_tile_") for source in sources):
        return "tiled_primary"
    return "primary+refinement"


def _merge_items(
    base_items: Sequence[Dict[str, Any]],
    refined_items: Sequence[Dict[str, Any]],
    conflict_delta: float,
) -> Tuple[List[Dict[str, Any]], int]:
    merged = [dict(item) for item in base_items]
    conflicts = 0
    for refined in refined_items:
        refined_box = _item_box(refined)
        if refined_box is None or not str(refined.get("text") or "").strip():
            continue
        matches: List[Tuple[int, float]] = []
        for index, current in enumerate(merged):
            current_box = _item_box(current)
            if current_box is None:
                continue
            overlap = _box_overlap(current_box, refined_box)
            if overlap >= 0.45:
                matches.append((index, overlap))
        if not matches:
            merged.append(dict(refined))
            continue
        # A refinement result that spans several primary blocks is not an
        # atomic alternative to any one of them. Keep the immutable primary
        # evidence instead of attaching an entire joined line to one block.
        if len(matches) > 1:
            continue
        best_index = matches[0][0]
        current = dict(merged[best_index])
        current_text = " ".join(str(current.get("text") or "").split())
        refined_text = " ".join(str(refined.get("text") or "").split())
        current_score = float(current.get("score") or 0.0)
        refined_score = float(refined.get("score") or 0.0)
        alternatives = list(current.get("alternatives") or [])
        if current_text == refined_text:
            if refined_score > current_score:
                current.update({key: value for key, value in refined.items() if key != "alternatives"})
            current["score"] = max(current_score, refined_score)
            current["recognition_source"] = _merged_recognition_source(current, refined)
            current["alternatives"] = alternatives
            merged[best_index] = current
            continue
        if abs(current_score - refined_score) < conflict_delta:
            added = _append_unique_alternative(alternatives, refined)
            current["alternatives"] = alternatives
            merged[best_index] = current
            if added:
                conflicts += 1
            continue
        if refined_score > current_score:
            replacement = dict(refined)
            replacement["alternatives"] = alternatives
            merged[best_index] = replacement
        else:
            current["alternatives"] = alternatives
            merged[best_index] = current
    return merged, conflicts


def build_captured_text_document(
    spans: Sequence[OcrSpan],
    image_size: Sequence[int],
) -> CapturedTextDocument:
    if len(image_size) != 2 or int(image_size[0]) < 1 or int(image_size[1]) < 1:
        raise ValueError("captured OCR image size must contain positive width and height")

    ordered = tuple(sorted(spans, key=lambda span: (span.line_id, span.box[0], span.id)))
    grouped: Dict[int, list[OcrSpan]] = {}
    for span in ordered:
        grouped.setdefault(span.line_id, []).append(span)

    lines = tuple(
        CapturedTextLine(
            line_id=line_id,
            text=" ".join(span.text for span in line_spans),
            span_ids=tuple(span.id for span in line_spans),
        )
        for line_id, line_spans in grouped.items()
    )
    return CapturedTextDocument(
        image_size=(int(image_size[0]), int(image_size[1])),
        lines=lines,
        spans=ordered,
        full_text="\n".join(line.text for line in lines),
        mean_confidence=average_score(ordered),
    )
