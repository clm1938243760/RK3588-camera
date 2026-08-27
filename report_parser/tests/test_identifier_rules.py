from __future__ import annotations

import unittest

from rk3588_report_parser.identifier_rules import (
    configured_primary_priority,
    matching_identifier_rules,
    matching_identifier_types,
    parse_identifier_rule_settings,
)


class IdentifierRuleTests(unittest.TestCase):
    def test_matches_length_charset_and_prefix(self) -> None:
        settings = parse_identifier_rule_settings(
            {
                "enabled": True,
                "profile": "hospital-a",
                "fields": [
                    {
                        "type": "patient_id",
                        "lengths": [11],
                        "charset": "digits",
                        "prefixes": [],
                        "priority": 80,
                    },
                    {
                        "type": "exam_request_no",
                        "lengths": [16],
                        "charset": "alphanumeric",
                        "prefixes": ["02D"],
                        "priority": 90,
                    },
                ],
            }
        )

        self.assertEqual(matching_identifier_types("60017095179", settings), ("patient_id",))
        self.assertFalse(matching_identifier_rules("60017095179", settings)[0].allow_unlabeled)
        self.assertEqual(matching_identifier_types("02D2026080500192", settings), ("exam_request_no",))
        self.assertEqual(matching_identifier_types("01D2026080500192", settings), ())
        self.assertEqual(configured_primary_priority(settings)[0], "exam_request_no")

    def test_same_format_can_match_multiple_types(self) -> None:
        settings = parse_identifier_rule_settings(
            {
                "enabled": True,
                "profile": "ambiguous",
                "fields": [
                    {"type": "patient_id", "lengths": [10], "charset": "digits", "priority": 10},
                    {"type": "inpatient_no", "lengths": [10], "charset": "digits", "priority": 20},
                ],
            }
        )

        self.assertEqual(
            matching_identifier_types("1234567890", settings),
            ("patient_id", "inpatient_no"),
        )

    def test_rejects_invalid_rule_configuration(self) -> None:
        with self.assertRaises(ValueError):
            parse_identifier_rule_settings(
                {
                    "enabled": True,
                    "profile": "bad",
                    "fields": [{"type": "phone", "lengths": [11], "charset": "digits"}],
                }
            )

        with self.assertRaises(ValueError):
            parse_identifier_rule_settings(
                {
                    "enabled": True,
                    "profile": "bad",
                    "fields": [
                        {
                            "type": "patient_id",
                            "lengths": [11],
                            "charset": "digits",
                            "allow_unlabeled": "yes",
                        }
                    ],
                }
            )


if __name__ == "__main__":
    unittest.main()
