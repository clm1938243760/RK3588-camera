from __future__ import annotations

from typing import List, Sequence

from .identifier_linker import BatchIdentifierLinker, BatchLinkOutcome, explicit_identifier_type
from .identifier_models import ClassifiedCandidate, IdentifierCandidate, UNKNOWN_IDENTIFIER_TYPE
from .identifier_rules import IdentifierRuleSettings, matching_identifier_rules
from .settings import LlmSettings


def link_identifiers_with_rules(
    model_linker: BatchIdentifierLinker,
    candidates: Sequence[IdentifierCandidate],
    llm_settings: LlmSettings,
    rule_settings: IdentifierRuleSettings,
) -> BatchLinkOutcome:
    if not rule_settings.enabled:
        return model_linker.link(candidates, llm_settings)

    linked: List[ClassifiedCandidate] = []
    unmatched: List[IdentifierCandidate] = []
    for candidate in candidates:
        matching_rules = matching_identifier_rules(candidate.value, rule_settings)
        matches = tuple(dict.fromkeys(rule.identifier_type for rule in matching_rules))
        if len(matches) == 1:
            identifier_type = matches[0]
            explicit_type = explicit_identifier_type(candidate.raw_label)
            prefix_supported = any(rule.prefixes for rule in matching_rules)
            unlabeled_allowed = any(rule.allow_unlabeled for rule in matching_rules)
            if explicit_type == identifier_type or prefix_supported or unlabeled_allowed:
                linked.append(
                    ClassifiedCandidate(
                        candidate,
                        identifier_type,
                        True,
                        decision_source="configured_rule",
                    )
                )
            else:
                reason = (
                    "configured_rule_label_conflict"
                    if explicit_type is not None
                    else "length_only_requires_review"
                )
                linked.append(
                    ClassifiedCandidate(
                        candidate,
                        UNKNOWN_IDENTIFIER_TYPE,
                        True,
                        review_reasons=(reason,),
                        decision_source="weak_rule_match",
                    )
                )
        elif len(matches) > 1:
            linked.append(
                ClassifiedCandidate(
                    candidate,
                    UNKNOWN_IDENTIFIER_TYPE,
                    True,
                    review_reasons=("ambiguous_rule_match",),
                    decision_source="ambiguous_rule_match",
                )
            )
        else:
            unmatched.append(candidate)
    for candidate in unmatched:
        linked.append(
            ClassifiedCandidate(
                candidate,
                UNKNOWN_IDENTIFIER_TYPE,
                True,
                review_reasons=("no_configured_rule_match",),
                decision_source="unmatched_rule",
            )
        )

    return BatchLinkOutcome(
        tuple(linked),
        0.0,
        0.0,
        "",
        "",
    )
