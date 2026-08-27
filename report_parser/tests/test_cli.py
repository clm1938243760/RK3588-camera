from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from rk3588_report_parser.cli import main


class CliTests(unittest.TestCase):
    def test_check_runtime_reports_unverified_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(json.dumps({"platform": "rk3588", "status": "unverified"}), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["--check-runtime", "--manifest", str(manifest)])

            result = json.loads(output.getvalue())
            self.assertEqual(code, 2)
            self.assertFalse(result["ok"])
            self.assertIn("manifest status must be verified", result["errors"])

    def test_missing_image_returns_operational_error(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--image", "C:/does/not/exist.jpg", "--allow-unverified-runtime"])

        result = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "error")


if __name__ == "__main__":
    unittest.main()
