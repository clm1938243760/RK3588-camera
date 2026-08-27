from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from rk3588_report_parser.manifest import check_manifest


class ManifestTests(unittest.TestCase):
    def test_verified_manifest_checks_artifact_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "librkllmrt.so"
            model = root / "model.rkllm"
            det = root / "ppocr_det.rknn"
            rec = root / "ppocr_rec.rknn"
            runtime.write_bytes(b"runtime")
            model.write_bytes(b"model")
            det.write_bytes(b"det")
            rec.write_bytes(b"rec")
            manifest = {
                "status": "verified",
                "platform": "rk3588",
                "rkllm": {
                    "sdk_version": "test-sdk",
                    "runtime_path": str(runtime),
                    "sha256": hashlib.sha256(b"runtime").hexdigest(),
                },
                "model": {
                    "target_platform": "rk3588",
                    "path": str(model),
                    "sha256": hashlib.sha256(b"model").hexdigest(),
                },
                "ppocr": {
                    "service_version": "test-ppocr",
                    "det_model_path": str(det),
                    "det_model_sha256": hashlib.sha256(b"det").hexdigest(),
                    "rec_model_path": str(rec),
                    "rec_model_sha256": hashlib.sha256(b"rec").hexdigest(),
                },
            }
            path = root / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")

            checked = check_manifest(path)

            self.assertTrue(checked["ok"], checked["errors"])

    def test_unverified_manifest_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps({"status": "unverified", "platform": "rk3588"}), encoding="utf-8")

            checked = check_manifest(path)

            self.assertFalse(checked["ok"])
            self.assertIn("manifest status must be verified", checked["errors"])


if __name__ == "__main__":
    unittest.main()
