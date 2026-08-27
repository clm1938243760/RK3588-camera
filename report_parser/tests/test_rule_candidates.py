from __future__ import annotations

import unittest

from rk3588_report_parser.identifier_candidates import CandidateSettings
from rk3588_report_parser.identifier_rules import parse_identifier_rule_settings
from rk3588_report_parser.models import OcrSpan
from rk3588_report_parser.rule_candidates import build_rule_identifier_candidates


def span(text: str) -> OcrSpan:
    return OcrSpan(1, 0, 1, text, (100, 100, 900, 140), (100, 100, 900, 140), 0.99)


class RuleCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = parse_identifier_rule_settings(
            {
                "enabled": True,
                "profile": "sample",
                "fields": [
                    {
                        "type": "exam_request_no",
                        "lengths": [12],
                        "charset": "alphanumeric",
                        "prefixes": ["01D"],
                    },
                    {
                        "type": "imaging_no",
                        "lengths": [11],
                        "charset": "digits",
                        "allow_unlabeled": True,
                    },
                ],
            }
        )

    def test_splits_two_identifiers_joined_in_one_ocr_span(self) -> None:
        candidates = build_rule_identifier_candidates(
            [span("01D11555114532607105741")], self.rules, CandidateSettings()
        )

        self.assertEqual(
            [candidate.value for candidate in candidates],
            ["01D115551145", "32607105741"],
        )
        self.assertTrue(all(candidate.value_mode == "rule_segment" for candidate in candidates))

    def test_splits_space_separated_identifiers(self) -> None:
        candidates = build_rule_identifier_candidates(
            [span("01D115551143 32607105742")], self.rules, CandidateSettings()
        )

        self.assertEqual(
            [candidate.value for candidate in candidates],
            ["01D115551143", "32607105742"],
        )
        self.assertEqual(candidates[1].raw_label, "")

    def test_retains_malformed_known_prefix_for_review(self) -> None:
        candidates = build_rule_identifier_candidates(
            [span("01D1155114432607105716做前列腺B超")], self.rules, CandidateSettings()
        )

        self.assertEqual([candidate.value for candidate in candidates], ["01D1155114432607105716"])

    def test_single_target_mode_keeps_only_the_configured_character_count(self) -> None:
        rules = parse_identifier_rule_settings(
            {
                "enabled": True,
                "profile": "single",
                "fields": [
                    {
                        "type": "selected_identifier",
                        "lengths": [8],
                        "charset": "alphanumeric",
                        "allow_unlabeled": True,
                    }
                ],
            }
        )
        candidates = build_rule_identifier_candidates(
            [span("检验/放射科P2540558 01D115551153 32607105742")],
            rules,
            CandidateSettings(),
        )

        self.assertEqual([candidate.value for candidate in candidates], ["P2540558"])

    def test_single_target_mode_does_not_apply_phone_or_confidence_filters(self) -> None:
        rules = parse_identifier_rule_settings(
            {
                "enabled": True,
                "profile": "single",
                "fields": [
                    {
                        "type": "selected_identifier",
                        "lengths": [11],
                        "charset": "alphanumeric",
                        "allow_unlabeled": True,
                    }
                ],
            }
        )
        low_confidence = OcrSpan(
            1,
            0,
            1,
            "号码13800138000",
            (100, 100, 900, 140),
            (100, 100, 900, 140),
            0.20,
        )

        candidates = build_rule_identifier_candidates(
            [low_confidence],
            rules,
            CandidateSettings(minimum_ocr_score=0.95),
        )

        self.assertEqual([candidate.value for candidate in candidates], ["13800138000"])


if __name__ == "__main__":
    unittest.main()
