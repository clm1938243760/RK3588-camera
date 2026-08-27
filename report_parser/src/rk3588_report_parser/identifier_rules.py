from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Sequence, Tuple

from .identifier_models import CORE_IDENTIFIER_TYPES, OTHER_IDENTIFIER_TYPE, PRIMARY_PRIORITY


RULE_CHARSETS: Tuple[str, ...] = ("digits", "alphanumeric")


@dataclass(frozen=True)
class IdentifierRule:
    identifier_type: str
    lengths: Tuple[int, ...]
    charset: str
    prefixes: Tuple[str, ...] = ()
    priority: int = 0
    enabled: bool = True
    allow_unlabeled: bool = False

    def matches(self, value: str) -> bool:
        if not self.enabled:
            return False
        compact = re.sub(r"\s+", "", value or "")
        if len(compact) not in self.lengths:
            return False
        if self.charset == "digits" and not compact.isdigit():
            return False
        if self.charset == "alphanumeric" and re.fullmatch(r"[A-Za-z0-9]+", compact) is None:
            return False
        return not self.prefixes or compact.upper().startswith(self.prefixes)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.identifier_type,
            "lengths": list(self.lengths),
            "charset": self.charset,
            "prefixes": list(self.prefixes),
            "priority": self.priority,
            "enabled": self.enabled,
            "allow_unlabeled": self.allow_unlabeled,
        }


@dataclass(frozen=True)
class IdentifierRuleSettings:
    enabled: bool = False
    profile: str = "unconfigured"
    fields: Tuple[IdentifierRule, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "profile": self.profile,
            "fields": [field.to_dict() for field in self.fields],
        }

    def summary(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "profile": self.profile,
            "field_count": sum(field.enabled for field in self.fields),
        }


def uses_character_count_only(settings: IdentifierRuleSettings) -> bool:
    enabled_rules = tuple(rule for rule in settings.fields if rule.enabled)
    return (
        settings.enabled
        and bool(enabled_rules)
        and all(rule.identifier_type == "selected_identifier" for rule in enabled_rules)
    )


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError("%s must be an integer from %d to %d" % (name, minimum, maximum))
    return value


def parse_identifier_rule_settings(payload: Mapping[str, Any]) -> IdentifierRuleSettings:
    if not isinstance(payload, Mapping):
        raise ValueError("identifier_rules must be an object")
    unknown = set(payload) - {"enabled", "profile", "fields"}
    if unknown:
        raise ValueError("unsupported identifier_rules config: %s" % ", ".join(sorted(unknown)))
    enabled = payload.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("identifier_rules.enabled must be a boolean")
    profile = payload.get("profile", "unconfigured")
    if not isinstance(profile, str) or not profile.strip() or len(profile.strip()) > 80:
        raise ValueError("identifier_rules.profile must be a non-empty string up to 80 characters")
    raw_fields = payload.get("fields", [])
    if not isinstance(raw_fields, list) or len(raw_fields) > 64:
        raise ValueError("identifier_rules.fields must be an array with at most 64 rules")

    fields = []
    for index, raw in enumerate(raw_fields):
        if not isinstance(raw, Mapping):
            raise ValueError("identifier_rules.fields[%d] must be an object" % index)
        allowed_keys = {
            "type",
            "lengths",
            "charset",
            "prefixes",
            "priority",
            "enabled",
            "allow_unlabeled",
        }
        extra = set(raw) - allowed_keys
        if extra:
            raise ValueError("identifier rule %d has unsupported keys: %s" % (index, ", ".join(sorted(extra))))
        identifier_type = raw.get("type")
        if identifier_type not in CORE_IDENTIFIER_TYPES + (OTHER_IDENTIFIER_TYPE,):
            raise ValueError("identifier rule %d has an unsupported type" % index)
        raw_lengths = raw.get("lengths")
        if not isinstance(raw_lengths, list) or not raw_lengths or len(raw_lengths) > 61:
            raise ValueError("identifier rule %d needs one or more lengths" % index)
        lengths = tuple(sorted({_integer(value, "rule length", 4, 64) for value in raw_lengths}))
        charset = raw.get("charset")
        if charset not in RULE_CHARSETS:
            raise ValueError("identifier rule %d charset must be digits or alphanumeric" % index)
        raw_prefixes = raw.get("prefixes", [])
        if not isinstance(raw_prefixes, list) or len(raw_prefixes) > 32:
            raise ValueError("identifier rule %d prefixes must be an array" % index)
        prefixes = []
        for prefix in raw_prefixes:
            if not isinstance(prefix, str) or not prefix.strip() or len(prefix.strip()) > 24:
                raise ValueError("identifier rule %d has an invalid prefix" % index)
            if re.fullmatch(r"[A-Za-z0-9]+", prefix.strip()) is None:
                raise ValueError("identifier rule %d prefix must be alphanumeric" % index)
            prefixes.append(prefix.strip().upper())
        priority = _integer(raw.get("priority", 0), "rule priority", 0, 1000)
        rule_enabled = raw.get("enabled", True)
        if not isinstance(rule_enabled, bool):
            raise ValueError("identifier rule %d enabled must be a boolean" % index)
        allow_unlabeled = raw.get("allow_unlabeled", False)
        if not isinstance(allow_unlabeled, bool):
            raise ValueError("identifier rule %d allow_unlabeled must be a boolean" % index)
        fields.append(
            IdentifierRule(
                identifier_type=str(identifier_type),
                lengths=lengths,
                charset=str(charset),
                prefixes=tuple(dict.fromkeys(prefixes)),
                priority=priority,
                enabled=rule_enabled,
                allow_unlabeled=allow_unlabeled,
            )
        )
    return IdentifierRuleSettings(enabled=enabled, profile=profile.strip(), fields=tuple(fields))


def matching_identifier_types(value: str, settings: IdentifierRuleSettings) -> Tuple[str, ...]:
    if not settings.enabled:
        return ()
    matches = {rule.identifier_type for rule in settings.fields if rule.matches(value)}
    ordered_types = CORE_IDENTIFIER_TYPES + (OTHER_IDENTIFIER_TYPE,)
    return tuple(identifier_type for identifier_type in ordered_types if identifier_type in matches)


def matching_identifier_rules(value: str, settings: IdentifierRuleSettings) -> Tuple[IdentifierRule, ...]:
    if not settings.enabled:
        return ()
    return tuple(rule for rule in settings.fields if rule.matches(value))


def configured_primary_priority(settings: IdentifierRuleSettings) -> Tuple[str, ...]:
    priority_by_type = {
        identifier_type: max(
            (rule.priority for rule in settings.fields if rule.enabled and rule.identifier_type == identifier_type),
            default=-1,
        )
        for identifier_type in CORE_IDENTIFIER_TYPES
    }
    fallback_index = {identifier_type: index for index, identifier_type in enumerate(PRIMARY_PRIORITY)}
    return tuple(
        sorted(
            CORE_IDENTIFIER_TYPES,
            key=lambda identifier_type: (-priority_by_type[identifier_type], fallback_index[identifier_type]),
        )
    )
