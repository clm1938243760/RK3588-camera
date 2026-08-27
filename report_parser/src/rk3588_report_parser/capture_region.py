"""Crop one normalized document region while retaining full-page OCR coordinates."""

from __future__ import annotations

import io
import math
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Sequence, Tuple

from PIL import Image

from .capture_text import FullTextExtraction, build_captured_text_document
from .models import OcrSpan, average_score


@dataclass(frozen=True)
class DocumentRecognitionRegion:
    crop_left: float = 0.0
    crop_top: float = 0.0
    crop_right: float = 1.0
    crop_bottom: float = 1.0
    accept_left: float = 0.0
    accept_top: float = 0.0
    accept_right: float = 1.0
    accept_bottom: float = 1.0

    def __post_init__(self) -> None:
        values = (
            self.crop_left,
            self.crop_top,
            self.crop_right,
            self.crop_bottom,
            self.accept_left,
            self.accept_top,
            self.accept_right,
            self.accept_bottom,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("recognition region values must be finite")
        if not (
            0.0 <= self.crop_left <= self.accept_left
            < self.accept_right <= self.crop_right <= 1.0
            and 0.0 <= self.crop_top <= self.accept_top
            < self.accept_bottom <= self.crop_bottom <= 1.0
        ):
            raise ValueError("accepted recognition region must be contained by the crop region")

    @property
    def enabled(self) -> bool:
        full_page = (0, 0, 1000, 1000)
        return self.crop_normalized != full_page or self.accept_normalized != full_page

    @property
    def crop_normalized(self) -> Tuple[int, int, int, int]:
        return _normalized_values(
            self.crop_left,
            self.crop_top,
            self.crop_right,
            self.crop_bottom,
        )

    @property
    def accept_normalized(self) -> Tuple[int, int, int, int]:
        return _normalized_values(
            self.accept_left,
            self.accept_top,
            self.accept_right,
            self.accept_bottom,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "coordinate_space": "rectified_document_normalized",
            "crop_normalized": list(self.crop_normalized),
            "accept_normalized": list(self.accept_normalized),
        }


@dataclass(frozen=True)
class CroppedDocument:
    image_bytes: bytes = field(repr=False)
    full_size: Tuple[int, int]
    crop_box: Tuple[int, int, int, int]
    accept_box: Tuple[int, int, int, int]
    region: DocumentRecognitionRegion


def crop_document_jpeg(
    image_bytes: bytes,
    region: DocumentRecognitionRegion,
) -> CroppedDocument:
    with Image.open(io.BytesIO(image_bytes)) as source:
        image = source.convert("RGB")
        try:
            full_size = image.size
            crop_box = _pixel_box(full_size, region.crop_normalized)
            accept_box = _pixel_box(full_size, region.accept_normalized)
            if not region.enabled:
                return CroppedDocument(image_bytes, full_size, crop_box, accept_box, region)
            cropped = image.crop(crop_box)
            try:
                output = io.BytesIO()
                cropped.save(output, format="JPEG", quality=95, subsampling=0)
            finally:
                cropped.close()
        finally:
            image.close()
    return CroppedDocument(output.getvalue(), full_size, crop_box, accept_box, region)


def remap_extraction_to_full_document(
    extraction: FullTextExtraction,
    mapping: CroppedDocument,
    low_confidence: float = 0.70,
    low_mean_confidence: float = 0.65,
) -> FullTextExtraction:
    if extraction.document is None:
        return extraction

    offset_x, offset_y = mapping.crop_box[:2]
    mapped = []
    for span in extraction.document.spans:
        full_span = _offset_span(span, offset_x, offset_y, mapping.full_size)
        center_x, center_y = full_span.center
        left, top, right, bottom = mapping.accept_box
        if left <= center_x < right and top <= center_y < bottom:
            mapped.append(full_span)

    reasons = [
        reason
        for reason in extraction.reasons
        if reason not in {"low_mean_confidence", "low_confidence_blocks"}
    ]
    if not mapped:
        reasons.append("no_ocr_text_blocks_in_recognition_region")
        return replace(
            extraction,
            status="rejected",
            document=None,
            reasons=tuple(dict.fromkeys(reasons)),
        )

    mean_confidence = average_score(mapped)
    if mean_confidence < low_mean_confidence:
        reasons.append("low_mean_confidence")
    if any(span.score < low_confidence for span in mapped):
        reasons.append("low_confidence_blocks")
    reasons = list(dict.fromkeys(reasons))
    return replace(
        extraction,
        status="review_required" if reasons else "accepted",
        document=build_captured_text_document(mapped, mapping.full_size),
        reasons=tuple(reasons),
    )


def _normalized_values(*values: float) -> Tuple[int, int, int, int]:
    normalized = tuple(max(0, min(1000, round(value * 1000))) for value in values)
    return (normalized[0], normalized[1], normalized[2], normalized[3])


def _pixel_box(
    image_size: Tuple[int, int],
    normalized_box: Sequence[int],
) -> Tuple[int, int, int, int]:
    width, height = image_size
    left = max(0, min(width - 1, math.floor(normalized_box[0] * width / 1000.0)))
    top = max(0, min(height - 1, math.floor(normalized_box[1] * height / 1000.0)))
    right = max(left + 1, min(width, math.ceil(normalized_box[2] * width / 1000.0)))
    bottom = max(top + 1, min(height, math.ceil(normalized_box[3] * height / 1000.0)))
    return (left, top, right, bottom)


def _normalize_point(point: Tuple[int, int], image_size: Tuple[int, int]) -> Tuple[int, int]:
    width, height = image_size
    return (
        max(0, min(1000, round(point[0] * 1000 / max(1, width)))),
        max(0, min(1000, round(point[1] * 1000 / max(1, height)))),
    )


def _offset_span(
    span: OcrSpan,
    offset_x: int,
    offset_y: int,
    image_size: Tuple[int, int],
) -> OcrSpan:
    width, height = image_size

    def point(value: Sequence[int]) -> Tuple[int, int]:
        return (
            max(0, min(width, int(value[0]) + offset_x)),
            max(0, min(height, int(value[1]) + offset_y)),
        )

    left, top = point((span.box[0], span.box[1]))
    right, bottom = point((span.box[2], span.box[3]))
    polygon = tuple(point(value) for value in span.polygon)
    if len(polygon) < 4:
        polygon = ((left, top), (right, top), (right, bottom), (left, bottom))
    normalized_box = (
        _normalize_point((left, top), image_size)
        + _normalize_point((right, bottom), image_size)
    )
    return replace(
        span,
        box=(left, top, right, bottom),
        normalized_box=normalized_box,
        polygon=polygon,
        normalized_polygon=tuple(_normalize_point(value, image_size) for value in polygon),
    )
