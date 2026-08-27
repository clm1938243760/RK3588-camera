"""In-memory burst quality scoring for document capture."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Tuple

from .paper_detector import PaperDetection
from .paper_trigger import BBox, bbox_iou


@dataclass(frozen=True)
class FrameQuality:
    sharpness: float
    edge_strength: float
    high_frequency_ratio: float
    glare_ratio: float
    motion_blur_risk: float
    contrast: float
    composite_score: float = 0.0

    def public_status(self) -> Dict[str, float]:
        return {
            "sharpness": round(self.sharpness, 2),
            "edge_strength": round(self.edge_strength, 2),
            "high_frequency_ratio": round(self.high_frequency_ratio, 4),
            "glare_ratio": round(self.glare_ratio, 4),
            "motion_blur_risk": round(self.motion_blur_risk, 4),
            "contrast": round(self.contrast, 2),
            "composite_score": round(self.composite_score, 4),
        }


@dataclass(frozen=True)
class BurstFrame:
    index: int
    timestamp: float
    image_bytes: bytes = field(repr=False)
    detection: PaperDetection = field(repr=False)
    quality: FrameQuality


@dataclass(frozen=True)
class BurstSelection:
    accepted: bool
    best_frame: BurstFrame
    frames: Tuple[BurstFrame, ...]
    rejection_reasons: Tuple[str, ...]

    def public_status(self) -> Dict[str, Any]:
        return {
            "ready": True,
            "accepted": self.accepted,
            "best_frame_index": self.best_frame.index,
            "rejection_reasons": list(self.rejection_reasons),
            "frames": [
                {"index": frame.index, **frame.quality.public_status()}
                for frame in self.frames
            ],
        }


class DocumentFrameQualityScorer:
    def __init__(
        self,
        analysis_longest_side: int = 960,
        glare_threshold: int = 250,
        cv2_module: Optional[Any] = None,
        numpy_module: Optional[Any] = None,
    ) -> None:
        if analysis_longest_side < 256:
            raise ValueError("analysis_longest_side must be at least 256")
        if not 1 <= glare_threshold <= 255:
            raise ValueError("glare_threshold must be in the range [1, 255]")
        if cv2_module is None:
            import cv2 as cv2_module
        if numpy_module is None:
            import numpy as numpy_module
        self.cv2 = cv2_module
        self.np = numpy_module
        self.analysis_longest_side = int(analysis_longest_side)
        self.glare_threshold = int(glare_threshold)

    def score_jpeg(self, image_bytes: bytes, detection: PaperDetection) -> FrameQuality:
        encoded = self.np.frombuffer(image_bytes, dtype=self.np.uint8)
        image = self.cv2.imdecode(encoded, self.cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("cannot decode capture JPEG")
        return self.score_image(image, detection)

    def score_image(self, image: Any, detection: PaperDetection) -> FrameQuality:
        if not detection.detected:
            raise ValueError("frame quality requires four document corners")
        height, width = image.shape[:2]
        scale = min(1.0, self.analysis_longest_side / max(width, height))
        if scale < 1.0:
            image = self.cv2.resize(
                image,
                (max(2, round(width * scale)), max(2, round(height * scale))),
                interpolation=self.cv2.INTER_AREA,
            )
        points = self.np.asarray(detection.corners, dtype=self.np.float32) * scale
        mask = self.np.zeros(image.shape[:2], dtype=self.np.uint8)
        self.cv2.fillPoly(mask, [self.np.rint(points).astype(self.np.int32)], 255)
        mask = self.cv2.erode(mask, self.np.ones((7, 7), dtype=self.np.uint8), iterations=1)
        selected = mask > 0
        if int(self.np.count_nonzero(selected)) < 1024:
            raise ValueError("detected document area is too small for quality scoring")

        gray = self.cv2.cvtColor(image, self.cv2.COLOR_BGR2GRAY).astype(self.np.float32)
        laplacian = self.cv2.Laplacian(gray, self.cv2.CV_32F, ksize=3)
        gradient_x = self.cv2.Sobel(gray, self.cv2.CV_32F, 1, 0, ksize=3)
        gradient_y = self.cv2.Sobel(gray, self.cv2.CV_32F, 0, 1, ksize=3)
        gradient_energy = gradient_x * gradient_x + gradient_y * gradient_y
        low_frequency = self.cv2.GaussianBlur(gray, (0, 0), 1.2)
        high_frequency = self.np.abs(gray - low_frequency)

        document_gray = gray[selected]
        sharpness = float(self.np.var(laplacian[selected]))
        edge_strength = float(self.np.mean(gradient_energy[selected]))
        contrast = float(self.np.std(document_gray))
        high_frequency_ratio = float(
            self.np.mean(high_frequency[selected]) / max(contrast, 1.0)
        )
        glare = self.np.all(image >= self.glare_threshold, axis=2)
        glare_ratio = float(self.np.mean(glare[selected]))
        motion_blur_risk = 1.0 / (
            1.0 + sharpness / 80.0 + high_frequency_ratio * 20.0
        )
        return FrameQuality(
            sharpness=sharpness,
            edge_strength=edge_strength,
            high_frequency_ratio=high_frequency_ratio,
            glare_ratio=glare_ratio,
            motion_blur_risk=motion_blur_risk,
            contrast=contrast,
        )


class BurstQualitySelector:
    def __init__(
        self,
        reference: PaperDetection,
        target_frames: int = 2,
        min_iou: float = 0.85,
        max_center_shift_ratio: float = 0.05,
        min_sharpness: float = 20.0,
        max_glare_ratio: float = 0.85,
        scorer: Optional[DocumentFrameQualityScorer] = None,
    ) -> None:
        if not reference.detected:
            raise ValueError("burst reference must contain a detected document")
        if target_frames < 2:
            raise ValueError("target_frames must be at least two")
        self.reference = reference
        self.target_frames = int(target_frames)
        self.min_iou = float(min_iou)
        self.max_center_shift_ratio = float(max_center_shift_ratio)
        self.min_sharpness = float(min_sharpness)
        self.max_glare_ratio = float(max_glare_ratio)
        self.scorer = scorer or DocumentFrameQualityScorer()
        self.frames: List[BurstFrame] = []
        self.rejected_frames = 0
        self.geometry_resets = 0
        self.quality_failures = 0
        self.last_quality_error: Optional[str] = None
        self.selection: Optional[BurstSelection] = None

    def add_frame(
        self,
        image_bytes: bytes,
        detection: PaperDetection,
        timestamp: float,
    ) -> Optional[BurstSelection]:
        if self.selection is not None:
            return self.selection
        if not self._same_paper(detection):
            self.rejected_frames += 1
            if detection.detected:
                self.geometry_resets += 1
                self.reference = detection
                self.frames.clear()
            return None
        try:
            quality = self.scorer.score_jpeg(image_bytes, detection)
        except (TypeError, ValueError) as exc:
            self.quality_failures += 1
            self.last_quality_error = str(exc)
            return None
        self.frames.append(
            BurstFrame(
                index=len(self.frames),
                timestamp=float(timestamp),
                image_bytes=image_bytes,
                detection=detection,
                quality=quality,
            )
        )
        if len(self.frames) < self.target_frames:
            return None
        self.selection = self._select()
        return self.selection

    def public_status(self) -> Dict[str, Any]:
        if self.selection is not None:
            status = self.selection.public_status()
            status["rejected_frames"] = self.rejected_frames
            status["geometry_resets"] = self.geometry_resets
            status["quality_failures"] = self.quality_failures
            return status
        return {
            "ready": False,
            "accepted": False,
            "collected_frames": len(self.frames),
            "target_frames": self.target_frames,
            "rejected_frames": self.rejected_frames,
            "geometry_resets": self.geometry_resets,
            "quality_failures": self.quality_failures,
            "last_quality_error": self.last_quality_error,
        }

    def _same_paper(self, detection: PaperDetection) -> bool:
        if not detection.detected:
            return False
        reference_bbox = _normalized_bbox(self.reference)
        current_bbox = _normalized_bbox(detection)
        iou = bbox_iou(reference_bbox, current_bbox)
        reference_center = _bbox_center(reference_bbox)
        current_center = _bbox_center(current_bbox)
        center_shift = math.hypot(
            current_center[0] - reference_center[0],
            current_center[1] - reference_center[1],
        )
        return iou >= self.min_iou and center_shift <= self.max_center_shift_ratio

    def _select(self) -> BurstSelection:
        sharpness = _normalize([frame.quality.sharpness for frame in self.frames])
        edges = _normalize([frame.quality.edge_strength for frame in self.frames])
        frequencies = _normalize(
            [frame.quality.high_frequency_ratio for frame in self.frames]
        )
        glare = _normalize([frame.quality.glare_ratio for frame in self.frames])
        scored = []
        for index, frame in enumerate(self.frames):
            composite = (
                0.40 * sharpness[index]
                + 0.20 * edges[index]
                + 0.30 * frequencies[index]
                + 0.10 * (1.0 - glare[index])
            )
            scored.append(
                replace(frame, quality=replace(frame.quality, composite_score=composite))
            )
        best = max(scored, key=lambda frame: frame.quality.composite_score)
        reasons = []
        if best.quality.sharpness < self.min_sharpness:
            reasons.append("all_frames_blurry")
        if best.quality.glare_ratio > self.max_glare_ratio:
            reasons.append("all_frames_overexposed")
        return BurstSelection(
            accepted=not reasons,
            best_frame=best,
            frames=tuple(scored),
            rejection_reasons=tuple(reasons),
        )


def _normalized_bbox(detection: PaperDetection) -> BBox:
    xs = [point[0] / detection.frame_width for point in detection.corners]
    ys = [point[1] / detection.frame_height for point in detection.corners]
    return min(xs), min(ys), max(xs), max(ys)


def _bbox_center(box: BBox) -> Tuple[float, float]:
    return (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0


def _normalize(values: List[float]) -> List[float]:
    minimum = min(values)
    maximum = max(values)
    if maximum - minimum <= 1e-12:
        return [0.5 for _ in values]
    return [(value - minimum) / (maximum - minimum) for value in values]
