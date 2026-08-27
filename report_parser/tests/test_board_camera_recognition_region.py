from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "board_camera_ocr_overlay.py"
SPEC = importlib.util.spec_from_file_location("board_camera_ocr_overlay", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
OVERLAY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(OVERLAY)


def test_trigger_status_retains_a_valid_recognition_region() -> None:
    status = OVERLAY.normalize_trigger_status(
        {
            "frame_size": {"width": 3840, "height": 2160},
            "paper_detected": True,
            "paper_corners": [[100, 100], [3000, 100], [3000, 1800], [100, 1800]],
            "ocr_rotation": 90,
            "recognition_region": {
                "enabled": True,
                "crop_normalized": [0, 130, 1000, 600],
                "accept_normalized": [0, 130, 1000, 600],
            },
        }
    )

    assert status["ocr_rotation"] == 90
    assert status["recognition_region"] == {
        "enabled": True,
        "coordinate_space": "rectified_document_normalized",
        "crop_normalized": [0, 130, 1000, 600],
        "accept_normalized": [0, 130, 1000, 600],
    }


def test_invalid_recognition_region_is_hidden() -> None:
    assert OVERLAY._safe_recognition_region(
        {
            "enabled": True,
            "crop_normalized": [0, 100, 1000, 600],
            "accept_normalized": [0, 80, 1000, 660],
        }
    ) == {"enabled": False}


def test_page_draws_a_translucent_perspective_mapped_region() -> None:
    page = OVERLAY.PAGE.decode("utf-8")
    assert "function drawRecognitionRegion(result)" in page
    assert "polygonFor(region.crop_normalized)" in page
    assert "polygonFor(region.accept_normalized)" in page
    assert '"rgba(14,165,233,.11)"' in page
