from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from rk3588_report_parser.settings import load_settings


class SettingsTests(unittest.TestCase):
    def test_rejects_non_local_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"llm": {"endpoint": "https://example.com/v1/chat"}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_settings(path)

    def test_loads_partial_local_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"llm": {"model": "local-test"}}), encoding="utf-8")
            settings = load_settings(path)
            self.assertEqual(settings.llm.model, "local-test")
            self.assertEqual(settings.ocr.endpoint, "http://127.0.0.1:5002/ocr")

    def test_loads_identifier_rule_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "identifier_rules": {
                            "enabled": True,
                            "profile": "hospital-a",
                            "fields": [
                                {
                                    "type": "patient_id",
                                    "lengths": [11],
                                    "charset": "digits",
                                    "priority": 100,
                                    "allow_unlabeled": True,
                                }
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )

            settings = load_settings(path)

            self.assertTrue(settings.identifier_rules.enabled)
            self.assertEqual(settings.identifier_rules.fields[0].lengths, (11,))
            self.assertTrue(settings.identifier_rules.fields[0].allow_unlabeled)


if __name__ == "__main__":
    unittest.main()
