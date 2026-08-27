from __future__ import annotations

from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .identifier_candidates import CandidateSettings, is_identifier_value
from .identifier_models import (
    CORE_IDENTIFIER_TYPES,
    OTHER_IDENTIFIER_TYPE,
    PRIMARY_PRIORITY,
    UNKNOWN_IDENTIFIER_TYPE,
    ClassifiedCandidate,
    IdentifierEvidence,
)


def _evidence(item: ClassifiedCandidate, selected: bool) -> IdentifierEvidence:
    candidate = item.candidate
    reasons = list(item.reasons)
    if item.identifier_type != "selected_identifier" and not is_identifier_value(candidate.value):
        reasons.append("invalid_identifier_format")
    return IdentifierEvidence(
        type=item.identifier_type,
        value=candidate.value,
        raw_label=candidate.raw_label,
        label_span_ids=list(candidate.label_span_ids),
        value_span_ids=list(candidate.value_span_ids),
        ocr_confidence=candidate.ocr_confidence,
        relation=candidate.relation,
        normalized_distance=candidate.normalized_distance,
        label_box=list(candidate.label_box),
        value_boxes=[list(box) for box in candidate.value_boxes],
        selected_for_type=selected,
        validation_ok=not reasons,
        validation_reasons=reasons,
        decision_source=item.decision_source,
    )


def _merge_duplicate_group(items: Sequence[ClassifiedCandidate]) -> ClassifiedCandidate:
    ordered = sorted(items, key=lambda item: item.candidate.ranking_key())
    return ordered[0]


def adjudicate_identifiers(
    linked: Sequence[ClassifiedCandidate],
    settings: Optional[CandidateSettings] = None,
    primary_priority: Sequence[str] = PRIMARY_PRIORITY,
    include_all_values: bool = False,
    require_single_value: bool = False,
) -> Tuple[str, Optional[IdentifierEvidence], List[IdentifierEvidence], List[IdentifierEvidence], List[str], List[str]]:
    config = settings or CandidateSettings()
    review_reasons: List[str] = []
    rejection_reasons: List[str] = []
    alternatives: List[IdentifierEvidence] = []

    for item in linked:
        review_reasons.extend(item.review_reasons)

    confirmed = [
        item
        for item in linked
        if item.confirmed
        and (
            item.identifier_type == "selected_identifier"
            or is_identifier_value(item.candidate.value)
        )
    ]
    rejected_by_verifier = [item for item in linked if not item.confirmed]
    for item in rejected_by_verifier:
        alternatives.append(_evidence(item, False))
    if rejected_by_verifier:
        review_reasons.append("model_verification_conflict")

    deduplicated: List[ClassifiedCandidate] = []
    duplicate_groups: Dict[Tuple[str, str], List[ClassifiedCandidate]] = defaultdict(list)
    for item in confirmed:
        duplicate_groups[(item.identifier_type, item.candidate.value)].append(item)
    for items in duplicate_groups.values():
        best = _merge_duplicate_group(items)
        deduplicated.append(best)

    value_types: Dict[str, set] = defaultdict(set)
    for item in deduplicated:
        value_types[item.candidate.value].add(item.identifier_type)
    if any(len(types) > 1 for types in value_types.values()):
        review_reasons.append("same_value_has_incompatible_types")

    core_by_type: Dict[str, List[ClassifiedCandidate]] = defaultdict(list)
    for item in deduplicated:
        if item.identifier_type == OTHER_IDENTIFIER_TYPE:
            alternatives.append(_evidence(item, False))
            review_reasons.append("other_medical_id_requires_review")
        elif item.identifier_type == UNKNOWN_IDENTIFIER_TYPE:
            alternatives.append(_evidence(item, False))
            review_reasons.append("unknown_identifier_requires_review")
        elif item.identifier_type in CORE_IDENTIFIER_TYPES:
            core_by_type[item.identifier_type].append(item)

    selected: List[IdentifierEvidence] = []
    for identifier_type in CORE_IDENTIFIER_TYPES:
        items = sorted(core_by_type.get(identifier_type, []), key=lambda item: item.candidate.ranking_key())
        if not items:
            continue
        selected.append(_evidence(items[0], True))
        if include_all_values:
            selected.extend(_evidence(item, True) for item in items[1:])
        else:
            alternatives.extend(_evidence(item, False) for item in items[1:])
        if not include_all_values and len(items) > 1:
            first, second = items[0].candidate, items[1].candidate
            if (
                first.relation == second.relation
                and abs(first.ocr_confidence - second.ocr_confidence) < config.tie_confidence_delta
                and abs(first.normalized_distance - second.normalized_distance) < config.tie_distance_delta
            ):
                review_reasons.append("near_tie:%s" % identifier_type)

    if require_single_value and len(selected) > 1:
        for item in selected:
            item.selected_for_type = False
            alternatives.append(item)
        selected = []
        review_reasons.append("multiple_target_identifiers")

    primary: Optional[IdentifierEvidence] = None
    by_type: Dict[str, IdentifierEvidence] = {}
    for item in selected:
        by_type.setdefault(item.type, item)
    for identifier_type in primary_priority:
        if identifier_type in by_type:
            primary = by_type[identifier_type]
            primary.is_primary = True
            break

    if primary is None:
        if review_reasons:
            status = "review_required"
        else:
            status = "rejected"
            rejection_reasons.append("no_reliable_medical_identifier")
    elif review_reasons:
        status = "review_required"
    else:
        status = "accepted"

    selected.sort(key=lambda item: primary_priority.index(item.type))
    alternatives.sort(key=lambda item: (item.type, item.value, item.raw_label))
    return (
        status,
        primary,
        selected,
        alternatives,
        sorted(set(review_reasons)),
        sorted(set(rejection_reasons)),
    )
