from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from .identifier_candidates import CandidateSettings, is_identifier_value
from .identifier_models import IdentifierCandidate
from .identifier_rules import IdentifierRule, IdentifierRuleSettings
from .models import OcrSpan


ALPHANUMERIC_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/\-]{3,63}")


@dataclass(frozen=True)
class _TokenPart:
    value: str
    start: int
    end: int


def _enabled_rules(settings: IdentifierRuleSettings) -> Tuple[IdentifierRule, ...]:
    return tuple(rule for rule in settings.fields if rule.enabled)


def _matching_rules(value: str, rules: Sequence[IdentifierRule]) -> Tuple[IdentifierRule, ...]:
    return tuple(rule for rule in rules if rule.matches(value))


def _split_by_rules(token: str, rules: Sequence[IdentifierRule]) -> Optional[Tuple[_TokenPart, ...]]:
    solutions: List[Tuple[_TokenPart, ...]] = []

    def walk(offset: int, parts: Tuple[_TokenPart, ...]) -> None:
        if len(solutions) > 16 or len(parts) >= 4:
            return
        if offset == len(token):
            if len(parts) >= 2:
                solutions.append(parts)
            return
        lengths = sorted(
            {
                length
                for rule in rules
                for length in rule.lengths
                if offset + length <= len(token)
            },
            reverse=True,
        )
        for length in lengths:
            value = token[offset : offset + length]
            if _matching_rules(value, rules):
                walk(offset + length, parts + (_TokenPart(value, offset, offset + length),))

    walk(0, ())
    unique = {
        tuple((part.start, part.end) for part in solution): solution
        for solution in solutions
    }
    if len(unique) != 1:
        return None
    return next(iter(unique.values()))


def _sub_box(
    box: Tuple[int, int, int, int],
    text_length: int,
    start: int,
    end: int,
) -> Tuple[int, int, int, int]:
    left, top, right, bottom = box
    width = max(1, right - left)
    denominator = max(1, text_length)
    return (
        left + round(width * start / denominator),
        top,
        left + round(width * end / denominator),
        bottom,
    )


def _prefix_label(text: str, start: int) -> str:
    label = text[:start].strip(" \t:：#()（）[]【】,，;；")[-48:]
    return "" if is_identifier_value(label) else label


def _has_known_prefix(value: str, rules: Sequence[IdentifierRule]) -> bool:
    upper = value.upper()
    return any(prefix and upper.startswith(prefix) for rule in rules for prefix in rule.prefixes)


def _parts_for_run(
    value: str,
    rules: Sequence[IdentifierRule],
    keep_unmatched: bool,
    character_count_only: bool = False,
) -> Tuple[_TokenPart, ...]:
    if character_count_only:
        if re.fullmatch(r"[A-Za-z0-9]+", value) is None:
            return ()
    elif not is_identifier_value(value):
        return ()
    if _matching_rules(value, rules):
        return (_TokenPart(value, 0, len(value)),)
    split = _split_by_rules(value, rules)
    if split is not None:
        return split
    if keep_unmatched and _has_known_prefix(value, rules):
        return (_TokenPart(value, 0, len(value)),)
    if keep_unmatched and len(value) >= 8 and re.fullmatch(r"[A-Za-z0-9]+", value):
        return (_TokenPart(value, 0, len(value)),)
    return ()


def build_rule_identifier_candidates(
    spans: Sequence[OcrSpan],
    rule_settings: IdentifierRuleSettings,
    candidate_settings: Optional[CandidateSettings] = None,
) -> List[IdentifierCandidate]:
    config = candidate_settings or CandidateSettings()
    rules = _enabled_rules(rule_settings)
    if not rule_settings.enabled or not rules:
        return []

    single_target = bool(rules) and all(rule.identifier_type == "selected_identifier" for rule in rules)
    candidates: List[IdentifierCandidate] = []
    seen = set()
    for span in spans:
        if not single_target and span.score < config.minimum_ocr_score:
            continue
        for match in ALPHANUMERIC_RUN.finditer(span.text):
            token = match.group(0)
            for part in _parts_for_run(
                token,
                rules,
                keep_unmatched=not single_target,
                character_count_only=single_target,
            ):
                absolute_start = match.start() + part.start
                absolute_end = match.start() + part.end
                key = (span.id, absolute_start, absolute_end, part.value)
                if key in seen:
                    continue
                seen.add(key)
                value_box = _sub_box(
                    span.normalized_box,
                    len(span.text),
                    absolute_start,
                    absolute_end,
                )
                raw_label = _prefix_label(span.text, match.start()) if part.start == 0 else ""
                relation = "same_span" if raw_label else "unlabeled"
                candidates.append(
                    IdentifierCandidate(
                        id=0,
                        raw_label=raw_label,
                        value=part.value,
                        label_span_ids=(span.id,) if raw_label else (),
                        value_span_ids=(span.id,),
                        value_mode="rule_segment" if len(part.value) != len(token) else "full_span",
                        relation=relation,
                        normalized_distance=0.0 if raw_label else 1.0,
                        ocr_confidence=span.score,
                        reading_order=span.id,
                        label_box=span.normalized_box if raw_label else value_box,
                        value_boxes=(value_box,),
                    )
                )

    candidates.sort(key=lambda item: (item.reading_order, item.value_boxes[0][0], item.value))
    return [
        IdentifierCandidate(
            id=index,
            raw_label=item.raw_label,
            value=item.value,
            label_span_ids=item.label_span_ids,
            value_span_ids=item.value_span_ids,
            value_mode=item.value_mode,
            relation=item.relation,
            normalized_distance=item.normalized_distance,
            ocr_confidence=item.ocr_confidence,
            reading_order=item.reading_order,
            label_box=item.label_box,
            value_boxes=item.value_boxes,
        )
        for index, item in enumerate(candidates[: config.max_candidates], start=1)
    ]
