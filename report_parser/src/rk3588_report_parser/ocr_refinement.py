"""No-template OCR refinement for configured-length identifiers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from PIL import Image

from .identifier_rules import IdentifierRule, IdentifierRuleSettings
from .rule_candidates import build_rule_identifier_candidates
from .spans import build_spans


ALPHANUMERIC_RUN = re.compile(r"[A-Za-z0-9]+")


@dataclass(frozen=True)
class OcrRegion:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(1, self.right - self.left)

    @property
    def height(self) -> int:
        return max(1, self.bottom - self.top)


def _number(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _item_box(item: Mapping[str, Any]) -> Optional[Tuple[int, int, int, int]]:
    box = item.get("box")
    if isinstance(box, (list, tuple)) and len(box) >= 4:
        values = [_number(value) for value in box[:4]]
        if all(value is not None for value in values):
            left, top, right, bottom = values
            return (
                int(min(left, right)),
                int(min(top, bottom)),
                int(max(left, right)),
                int(max(top, bottom)),
            )
    return None


def _enabled_rules(settings: IdentifierRuleSettings) -> Tuple[IdentifierRule, ...]:
    return tuple(rule for rule in settings.fields if rule.enabled)


def _run_compatible(value: str, rules: Sequence[IdentifierRule]) -> bool:
    return any(
        (rule.charset == "digits" and value.isdigit())
        or (rule.charset == "alphanumeric" and value.isascii() and value.isalnum())
        for rule in rules
    )


def _clamp_region(region: OcrRegion, image_size: Tuple[int, int]) -> OcrRegion:
    width, height = image_size
    return OcrRegion(
        max(0, min(width - 1, region.left)),
        max(0, min(height - 1, region.top)),
        max(1, min(width, region.right)),
        max(1, min(height, region.bottom)),
    )


def _overlap_ratio(first: OcrRegion, second: OcrRegion) -> float:
    width = max(0, min(first.right, second.right) - max(first.left, second.left))
    height = max(0, min(first.bottom, second.bottom) - max(first.top, second.top))
    intersection = width * height
    minimum = min(first.width * first.height, second.width * second.height)
    return intersection / max(1, minimum)


def _merge_regions(regions: Iterable[OcrRegion], limit: int) -> List[OcrRegion]:
    merged: List[OcrRegion] = []
    for region in regions:
        for index, current in enumerate(merged):
            if _overlap_ratio(region, current) < 0.45:
                continue
            merged[index] = OcrRegion(
                min(region.left, current.left),
                min(region.top, current.top),
                max(region.right, current.right),
                max(region.bottom, current.bottom),
            )
            break
        else:
            merged.append(region)
        if len(merged) >= limit:
            break
    return merged


def candidate_regions(
    response: Mapping[str, Any],
    image_size: Tuple[int, int],
    settings: IdentifierRuleSettings,
    limit: int = 6,
) -> List[OcrRegion]:
    """Expand around partial number-like OCR rows without using page coordinates."""

    rules = _enabled_rules(settings)
    if not rules:
        return []
    target_lengths = sorted({length for rule in rules for length in rule.lengths})
    minimum_run = max(4, min(target_lengths) // 2)
    maximum_run = max(target_lengths) - 1
    ranked = []
    for index, item in enumerate(response.get("ocr") or []):
        if not isinstance(item, dict):
            continue
        box = _item_box(item)
        if box is None:
            continue
        text = str(item.get("text") or "")
        score = float(item.get("score") or 0.0)
        for match in ALPHANUMERIC_RUN.finditer(text):
            value = match.group(0)
            if not minimum_run <= len(value) <= maximum_run or not _run_compatible(value, rules):
                continue
            nearest = min(abs(length - len(value)) for length in target_lengths)
            ranked.append((nearest, -len(value), -score, index, box, len(value)))

    regions = []
    for _, _, _, _, box, run_length in sorted(ranked):
        left, top, right, bottom = box
        width = max(1, right - left)
        height = max(1, bottom - top)
        expected_width = width * max(target_lengths) / max(1, run_length)
        margin_x = round(max(32.0, height * 4.0, width * 0.85, expected_width - width))
        margin_y = round(max(32.0, height * 3.0))
        regions.append(
            _clamp_region(
                OcrRegion(left - margin_x, top - margin_y, right + margin_x, bottom + margin_y),
                image_size,
            )
        )
    return _merge_regions(regions, limit)


def _starts(length: int, tile: int, overlap: int) -> List[int]:
    if tile >= length:
        return [0]
    step = max(1, tile - overlap)
    starts = list(range(0, max(1, length - tile + 1), step))
    last = length - tile
    if not starts or starts[-1] != last:
        starts.append(last)
    return starts


def tiled_regions(image_size: Tuple[int, int], limit: int = 12) -> List[OcrRegion]:
    """Cover a document with overlapping OCR-sized regions as a final fallback."""

    width, height = image_size
    tile_width = min(width, 800)
    tile_height = min(height, max(420, round(tile_width * 0.75)))
    x_starts = _starts(width, tile_width, round(tile_width * 0.2))
    y_starts = _starts(height, tile_height, round(tile_height * 0.25))
    return [
        OcrRegion(left, top, left + tile_width, top + tile_height)
        for top in y_starts
        for left in x_starts
    ][:limit]


def _enlarge(image: Image.Image, region: OcrRegion) -> Tuple[Image.Image, float, float]:
    crop = image.crop((region.left, region.top, region.right, region.bottom))
    scale = min(4.0, 1600.0 / max(crop.size))
    if scale <= 1.01:
        return crop, 1.0, 1.0
    namespace = getattr(Image, "Resampling", Image)
    resized = crop.resize(
        (max(1, round(crop.width * scale)), max(1, round(crop.height * scale))),
        namespace.LANCZOS,
    )
    return resized, resized.width / crop.width, resized.height / crop.height


def _candidate_items(
    response: Dict[str, Any],
    rendered_size: Tuple[int, int],
    region: OcrRegion,
    scale_x: float,
    scale_y: float,
    settings: IdentifierRuleSettings,
) -> List[Dict[str, Any]]:
    spans = build_spans(response, rendered_size)
    candidates = build_rule_identifier_candidates(spans, settings)
    items = []
    for candidate in candidates:
        boxes = candidate.value_boxes
        if not boxes:
            continue
        left = min(box[0] for box in boxes) * rendered_size[0] / 1000.0
        top = min(box[1] for box in boxes) * rendered_size[1] / 1000.0
        right = max(box[2] for box in boxes) * rendered_size[0] / 1000.0
        bottom = max(box[3] for box in boxes) * rendered_size[1] / 1000.0
        items.append(
            {
                "text": candidate.value,
                "score": candidate.ocr_confidence,
                "box": [
                    round(region.left + left / scale_x, 2),
                    round(region.top + top / scale_y, 2),
                    round(region.left + right / scale_x, 2),
                    round(region.top + bottom / scale_y, 2),
                ],
            }
        )
    return items


def refine_configured_identifier_ocr(
    image: Image.Image,
    response: Dict[str, Any],
    settings: IdentifierRuleSettings,
    recognize: Callable[[Image.Image], Dict[str, Any]],
) -> Dict[str, Any]:
    """Add only exact configured-length values found in enlarged no-template crops."""

    initial_spans = build_spans(response, image.size)
    if build_rule_identifier_candidates(initial_spans, settings):
        return response

    calls = 0
    recovered: Dict[str, Dict[str, Any]] = {}

    def scan(regions: Sequence[OcrRegion]) -> None:
        nonlocal calls
        for region in regions:
            rendered, scale_x, scale_y = _enlarge(image, region)
            refined = recognize(rendered)
            calls += 1
            if refined.get("ok") is False or not isinstance(refined.get("ocr"), list):
                continue
            for item in _candidate_items(refined, rendered.size, region, scale_x, scale_y, settings):
                value = str(item["text"])
                current = recovered.get(value)
                if current is None or float(item["score"]) > float(current["score"]):
                    recovered[value] = item

    mode = "candidate_regions"
    scan(candidate_regions(response, image.size, settings))
    if not recovered:
        mode = "overlapping_tiles"
        scan(tiled_regions(image.size))

    merged = dict(response)
    merged["ocr"] = list(response.get("ocr") or []) + list(recovered.values())
    merged["refinement"] = {
        "mode": mode,
        "calls": calls,
        "recovered_values": len(recovered),
    }
    return merged
