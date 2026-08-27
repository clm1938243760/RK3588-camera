from __future__ import annotations

import unittest

from rk3588_report_parser.identifier_linker import BatchLinkOutcome
from rk3588_report_parser.identifier_models import ClassifiedCandidate, IdentifierCandidate
from rk3588_report_parser.identifier_rules import parse_identifier_rule_settings
from rk3588_report_parser.rule_linker import link_identifiers_with_rules
from rk3588_report_parser.settings import LlmSettings


def candidate(value="1234567890", raw_label=""):
    return IdentifierCandidate(
        1, raw_label, value, (), (1,), "full_span", "unlabeled", 1.0, 0.98, 1,
        (10, 10, 200, 40), ((10, 10, 200, 40),),
    )


class FakeModelLinker:
    def __init__(self):
        self.calls = 0

    def link(self, candidates, settings, allowed_types_by_id=None):
        self.calls += 1
        item = candidates[0]
        selected_type = allowed_types_by_id[item.id][0]
        return BatchLinkOutcome(
            (ClassifiedCandidate(item, selected_type, True),), 2.0, 3.0, "{}", "{}"
        )


class RuleLinkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.llm = LlmSettings("http://127.0.0.1:8010/v1/chat/completions", "test", 5, 128)

    def test_unique_rule_match_does_not_call_model(self) -> None:
        rules = parse_identifier_rule_settings(
            {
                "enabled": True,
                "profile": "unique",
                "fields": [{"type": "patient_id", "lengths": [10], "charset": "digits"}],
            }
        )
        model = FakeModelLinker()
        outcome = link_identifiers_with_rules(model, [candidate(raw_label="患者ID")], self.llm, rules)

        self.assertEqual(model.calls, 0)
        self.assertEqual(outcome.candidates[0].identifier_type, "patient_id")
        self.assertEqual(outcome.candidates[0].decision_source, "configured_rule")

    def test_length_only_match_requires_review_by_default(self) -> None:
        rules = parse_identifier_rule_settings(
            {
                "enabled": True,
                "profile": "safe-default",
                "fields": [{"type": "patient_id", "lengths": [10], "charset": "digits"}],
            }
        )
        model = FakeModelLinker()
        outcome = link_identifiers_with_rules(model, [candidate()], self.llm, rules)

        self.assertEqual(model.calls, 0)
        self.assertEqual(outcome.candidates[0].identifier_type, "unknown_identifier")
        self.assertIn("length_only_requires_review", outcome.candidates[0].review_reasons)
        self.assertEqual(outcome.candidates[0].decision_source, "weak_rule_match")

    def test_explicitly_allowed_unlabeled_match_is_confirmed(self) -> None:
        rules = parse_identifier_rule_settings(
            {
                "enabled": True,
                "profile": "opt-in",
                "fields": [
                    {
                        "type": "patient_id",
                        "lengths": [10],
                        "charset": "digits",
                        "allow_unlabeled": True,
                    }
                ],
            }
        )
        outcome = link_identifiers_with_rules(FakeModelLinker(), [candidate()], self.llm, rules)

        self.assertEqual(outcome.candidates[0].identifier_type, "patient_id")
        self.assertEqual(outcome.candidates[0].decision_source, "configured_rule")

    def test_explicit_label_conflict_requires_review(self) -> None:
        rules = parse_identifier_rule_settings(
            {
                "enabled": True,
                "profile": "conflict",
                "fields": [{"type": "patient_id", "lengths": [10], "charset": "digits"}],
            }
        )
        outcome = link_identifiers_with_rules(
            FakeModelLinker(), [candidate(raw_label="住院号")], self.llm, rules
        )

        self.assertEqual(outcome.candidates[0].identifier_type, "unknown_identifier")
        self.assertIn("configured_rule_label_conflict", outcome.candidates[0].review_reasons)

    def test_ambiguous_rule_match_does_not_use_model_and_requires_review(self) -> None:
        rules = parse_identifier_rule_settings(
            {
                "enabled": True,
                "profile": "ambiguous",
                "fields": [
                    {"type": "patient_id", "lengths": [10], "charset": "digits"},
                    {"type": "inpatient_no", "lengths": [10], "charset": "digits"},
                ],
            }
        )
        model = FakeModelLinker()
        outcome = link_identifiers_with_rules(model, [candidate()], self.llm, rules)

        self.assertEqual(model.calls, 0)
        self.assertEqual(outcome.candidates[0].identifier_type, "unknown_identifier")
        self.assertIn("ambiguous_rule_match", outcome.candidates[0].review_reasons)

    def test_unmatched_number_is_retained_as_unknown(self) -> None:
        rules = parse_identifier_rule_settings(
            {
                "enabled": True,
                "profile": "unmatched",
                "fields": [{"type": "patient_id", "lengths": [11], "charset": "digits"}],
            }
        )
        outcome = link_identifiers_with_rules(FakeModelLinker(), [candidate()], self.llm, rules)

        self.assertEqual(outcome.candidates[0].identifier_type, "unknown_identifier")
        self.assertIn("no_configured_rule_match", outcome.candidates[0].review_reasons)


if __name__ == "__main__":
    unittest.main()
