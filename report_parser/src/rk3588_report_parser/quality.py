from __future__ import annotations

import io
from typing import Tuple

from PIL import Image, ImageOps, ImageStat, UnidentifiedImageError

from .models import QualityAssessment
from .settings import QualitySettings


class InvalidImageError(ValueError):
    pass


def decode_image(image_bytes: bytes) -> Image.Image:
    if not image_bytes:
        raise InvalidImageError("image is empty")
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.load()
            return image.copy()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidImageError("image is not a readable JPEG or PNG") from exc


def encode_for_ocr(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95, optimize=False)
    return buffer.getvalue()


def _scaled_gray(image: Image.Image) -> Image.Image:
    gray = image.convert("L")
    longest = max(gray.size)
    if longest <= 768:
        return gray
    scale = 768.0 / longest
    return gray.resize((max(2, int(gray.width * scale)), max(2, int(gray.height * scale))))


def _pixels(gray: Image.Image):
    flattened = getattr(gray, "get_flattened_data", None)
    return list(flattened() if flattened is not None else gray.getdata())


def _laplacian_energy(gray: Image.Image) -> float:
    width, height = gray.size
    if width < 3 or height < 3:
        return 0.0
    pixels = _pixels(gray)
    total = 0.0
    count = 0
    for y in range(1, height - 1):
        row = y * width
        for x in range(1, width - 1):
            index = row + x
            laplacian = (
                pixels[index - 1]
                + pixels[index + 1]
                + pixels[index - width]
                + pixels[index + width]
                - 4 * pixels[index]
            )
            total += laplacian * laplacian
            count += 1
    return total / max(count, 1)


def assess_image(image: Image.Image, settings: QualitySettings, image_format: str = "JPEG") -> QualityAssessment:
    gray = _scaled_gray(image)
    stat = ImageStat.Stat(gray)
    mean = float(stat.mean[0])
    contrast = float(stat.stddev[0])
    pixels = _pixels(gray)
    total = max(len(pixels), 1)
    dark_ratio = sum(value <= 12 for value in pixels) / total
    bright_ratio = sum(value >= 248 for value in pixels) / total
    energy = _laplacian_energy(gray)
    reasons = []

    if max(image.size) < settings.min_longest_side:
        reasons.append("insufficient_resolution")
    if contrast < settings.min_contrast:
        reasons.append("low_contrast")
    if energy < settings.min_laplacian_energy:
        reasons.append("blurry")
    if mean <= 18 or dark_ratio >= 0.97:
        reasons.append("too_dark")
    if mean >= 250 or bright_ratio >= 0.995:
        reasons.append("overexposed")

    return QualityAssessment(
        ok=not reasons,
        image_size=image.size,
        image_format=image_format,
        metrics={
            "mean_luma": mean,
            "contrast": contrast,
            "laplacian_energy": energy,
            "dark_ratio": dark_ratio,
            "bright_ratio": bright_ratio,
        },
        reasons=reasons,
    )
