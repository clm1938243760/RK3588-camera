from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CameraRuntimeManifestTests(unittest.TestCase):
    def test_board_verified_detector_artifacts_are_locked(self) -> None:
        manifest = json.loads((ROOT / "runtime" / "manifest.json").read_text(encoding="utf-8"))
        detector = manifest["paper_detector"]
        model = ROOT / detector["path"]

        self.assertEqual(detector["status"], "board_verified")
        self.assertEqual(hashlib.sha256(model.read_bytes()).hexdigest(), detector["sha256"])
        self.assertGreater(detector["benchmark"]["iterations"], 0)
        self.assertLess(detector["benchmark"]["p95_inference_ms"], 200.0)

        requirements = (ROOT / "requirements-camera-trigger-board.txt").read_text(
            encoding="utf-8"
        )
        runtime = detector["runtime"]
        for key in ("numpy_wheel_sha256", "onnxruntime_wheel_sha256"):
            digest = runtime[key]
            self.assertRegex(digest, re.compile(r"^[0-9a-f]{64}$"))
            self.assertIn("--hash=sha256:%s" % digest, requirements)

    def test_text_only_service_uses_two_frames_and_a_ten_second_budget(self) -> None:
        unit = (ROOT / "systemd" / "rk3588-report-camera-trigger.service").read_text(
            encoding="utf-8"
        )

        self.assertIn("--text-only", unit)
        self.assertIn("--burst-frames 2", unit)
        self.assertIn("--ocr-total-budget 10", unit)
        self.assertIn("--ocr-tile-max-aspect 0", unit)
        self.assertIn("--ocr-refinement-max-regions 0", unit)
        self.assertIn("Environment=OCR_REGION_TOP=0.13", unit)
        self.assertIn("Environment=OCR_REGION_BOTTOM=0.60", unit)
        self.assertIn("--ocr-region-crop-top ${OCR_REGION_TOP}", unit)
        self.assertIn("--ocr-region-crop-bottom ${OCR_REGION_BOTTOM}", unit)
        self.assertIn("--ocr-region-accept-top ${OCR_REGION_TOP}", unit)
        self.assertIn("--ocr-region-accept-bottom ${OCR_REGION_BOTTOM}", unit)

        manifest = json.loads((ROOT / "runtime" / "manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["deployment"]["ocr_tiling"]["enabled"])
        self.assertEqual(manifest["deployment"]["ocr_tiling"]["max_aspect"], 0.0)
        region = manifest["deployment"]["ocr_document_region"]
        self.assertEqual(region["crop_normalized"], [0, 130, 1000, 600])
        self.assertEqual(region["accept_normalized"], [0, 130, 1000, 600])
        acceptance = region["board_acceptance"]
        self.assertEqual(acceptance["status"], "board_verified")
        self.assertEqual(acceptance["repeats"], 5)
        self.assertEqual(acceptance["ocr_call_counts"], [1, 1, 1, 1, 1])
        self.assertEqual(acceptance["refinement_ocr_call_counts"], [0, 0, 0, 0, 0])
        self.assertEqual(acceptance["secondary_full_ocr_call_counts"], [0, 0, 0, 0, 0])
        self.assertLess(acceptance["maximum_ocr_total_ms"], 800.0)
        self.assertLess(acceptance["latest_maximum_block_center_y"], 600.0)
        self.assertTrue(
            all(value == 5 for value in acceptance["required_section_matches"].values())
        )
        self.assertEqual(manifest["deployment"]["ocr_refinement"]["max_regions"], 0)
        self.assertTrue(
            manifest["deployment"]["ocr_refinement"]["empty_primary_second_frame_retry"]
        )

    def test_ppocr_worker_is_locked_to_verified_three_core_parallel_runtime(self) -> None:
        manifest = json.loads((ROOT / "runtime" / "manifest.json").read_text(encoding="utf-8"))
        ppocr = manifest["ppocr"]
        execution = ppocr["recognition_execution"]

        self.assertEqual(ppocr["service_version"], "ppocrv4-rknn-parallel3-20260824")
        self.assertEqual(
            ppocr["worker_sha256"],
            "b2a1ff37fbd2d16e03f96714593963fa7b23f5ce8eae1892f4616c12cb8b14fa",
        )
        self.assertEqual(execution["model_batch_size"], 1)
        self.assertEqual(execution["parallel_contexts"], 3)
        self.assertEqual(execution["context_api"], "rknn_dup_context")
        self.assertEqual(
            execution["core_masks"],
            ["RKNN_NPU_CORE_0", "RKNN_NPU_CORE_1", "RKNN_NPU_CORE_2"],
        )
        self.assertEqual(execution["batch4_candidate"]["status"], "rejected")
        self.assertGreaterEqual(execution["benchmark"]["iterations"], 9)
        self.assertEqual(execution["benchmark"]["full_pipeline_blocks"], 39)
        self.assertLess(
            execution["benchmark"]["full_pipeline_ocr_total_after_ms"],
            execution["benchmark"]["full_pipeline_ocr_total_before_ms"],
        )
        live = execution["benchmark"]["camera_e2e_repeats"]
        self.assertEqual(live["iterations"], 3)
        self.assertEqual(len(live["ocr_total_ms"]), live["iterations"])
        self.assertLess(max(live["ocr_total_ms"]), 3000.0)
        self.assertEqual(live["text_stability"], "frame_dependent")


if __name__ == "__main__":
    unittest.main()
