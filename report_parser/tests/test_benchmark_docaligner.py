from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_docaligner.py"
SPEC = importlib.util.spec_from_file_location("benchmark_docaligner", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class BenchmarkDocAlignerTests(unittest.TestCase):
    def test_percentile_is_deterministic_for_short_samples(self) -> None:
        values = [10.0, 30.0, 20.0, 50.0, 40.0]
        self.assertEqual(MODULE.percentile(values, 0.50), 30.0)
        self.assertEqual(MODULE.percentile(values, 0.95), 50.0)

    def test_percentile_rejects_empty_input(self) -> None:
        with self.assertRaises(ValueError):
            MODULE.percentile([], 0.50)


if __name__ == "__main__":
    unittest.main()
