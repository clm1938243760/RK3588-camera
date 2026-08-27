from __future__ import annotations

import copy
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from .identifier_candidates import CandidateSettings
from .identifier_rules import IdentifierRuleSettings, parse_identifier_rule_settings
from .preprocessing import PreprocessSettings


DEFAULT_CONFIG: Dict[str, Any] = {
    "ocr": {
        "endpoint": "http://127.0.0.1:5002/ocr",
        "timeout_seconds": 20.0,
    },
    "llm": {
        "endpoint": "http://127.0.0.1:8010/v1/chat/completions",
        "model": "qwen2.5-1.5b-instruct-rk3588",
        "timeout_seconds": 90.0,
        "max_tokens": 512,
    },
    "quality": {
        "min_longest_side": 1600,
        "min_contrast": 6.0,
        "min_laplacian_energy": 25.0,
        "min_ocr_items": 3,
        "min_ocr_score": 0.65,
    },
    "preprocessing": {
        "perspective_correction": True,
        "min_document_area_ratio": 0.25,
        "min_confidence": 0.82,
        "min_output_side": 320,
    },
    "validation": {
        "max_age_delta_years": 2,
        "require_patient_name": True,
        "require_identifier": True,
    },
    "identifiers": {
        "profile": "edge-rk3588",
        "max_candidates": 96,
        "minimum_ocr_score": 0.65,
        "same_line_gap": 220.0,
        "next_line_gap": 120.0,
        "nearby_distance": 220.0,
        "tie_confidence_delta": 0.03,
        "tie_distance_delta": 0.02,
    },
    "identifier_rules": {
        "enabled": False,
        "profile": "unconfigured",
        "fields": [],
    },
}


@dataclass(frozen=True)
class OcrSettings:
    endpoint: str
    timeout_seconds: float


@dataclass(frozen=True)
class LlmSettings:
    endpoint: str
    model: str
    timeout_seconds: float
    max_tokens: int


@dataclass(frozen=True)
class QualitySettings:
    min_longest_side: int
    min_contrast: float
    min_laplacian_energy: float
    min_ocr_items: int
    min_ocr_score: float


@dataclass(frozen=True)
class ValidationSettings:
    max_age_delta_years: int
    require_patient_name: bool
    require_identifier: bool


@dataclass(frozen=True)
class ParserSettings:
    ocr: OcrSettings
    llm: LlmSettings
    quality: QualitySettings
    validation: ValidationSettings
    identifiers: CandidateSettings = CandidateSettings()
    profile: str = "edge-rk3588"
    preprocessing: PreprocessSettings = PreprocessSettings()
    identifier_rules: IdentifierRuleSettings = IdentifierRuleSettings()


def _merge(base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(base)
    for section, values in incoming.items():
        if section not in merged:
            raise ValueError("unsupported config section: %s" % section)
        if not isinstance(values, dict):
            raise ValueError("%s must be an object" % section)
        unknown = set(values) - set(merged[section])
        if unknown:
            raise ValueError("unsupported %s config: %s" % (section, ", ".join(sorted(unknown))))
        merged[section].update(values)
    return merged


def _local_http_url(value: Any, name: str) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("%s must be an HTTP URL" % name)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("%s must point to a local service" % name)
    return url


def _positive_number(value: Any, name: str, minimum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("%s must be a number" % name) from exc
    if number < minimum:
        raise ValueError("%s must be >= %s" % (name, minimum))
    return number


def load_settings(config_path: Optional[Path] = None) -> ParserSettings:
    raw: Dict[str, Any] = {}
    if config_path is not None:
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ValueError("config file not found: %s" % config_path)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid config JSON: %s" % exc) from exc
        if not isinstance(raw, dict):
            raise ValueError("config root must be an object")
    merged = _merge(DEFAULT_CONFIG, raw)

    ocr = merged["ocr"]
    llm = merged["llm"]
    quality = merged["quality"]
    validation = merged["validation"]
    identifiers = merged["identifiers"]
    preprocessing = merged["preprocessing"]
    identifier_rules = merged["identifier_rules"]

    model = str(llm["model"]).strip()
    if not model:
        raise ValueError("llm.model must not be empty")

    return ParserSettings(
        ocr=OcrSettings(
            endpoint=_local_http_url(ocr["endpoint"], "ocr.endpoint"),
            timeout_seconds=_positive_number(ocr["timeout_seconds"], "ocr.timeout_seconds", 1),
        ),
        llm=LlmSettings(
            endpoint=_local_http_url(llm["endpoint"], "llm.endpoint"),
            model=model,
            timeout_seconds=_positive_number(llm["timeout_seconds"], "llm.timeout_seconds", 1),
            max_tokens=int(_positive_number(llm["max_tokens"], "llm.max_tokens", 16)),
        ),
        quality=QualitySettings(
            min_longest_side=int(_positive_number(quality["min_longest_side"], "quality.min_longest_side", 64)),
            min_contrast=_positive_number(quality["min_contrast"], "quality.min_contrast", 0),
            min_laplacian_energy=_positive_number(
                quality["min_laplacian_energy"], "quality.min_laplacian_energy", 0
            ),
            min_ocr_items=int(_positive_number(quality["min_ocr_items"], "quality.min_ocr_items", 1)),
            min_ocr_score=_positive_number(quality["min_ocr_score"], "quality.min_ocr_score", 0),
        ),
        validation=ValidationSettings(
            max_age_delta_years=int(
                _positive_number(validation["max_age_delta_years"], "validation.max_age_delta_years", 0)
            ),
            require_patient_name=bool(validation["require_patient_name"]),
            require_identifier=bool(validation["require_identifier"]),
        ),
        identifiers=CandidateSettings(
            max_candidates=int(_positive_number(identifiers["max_candidates"], "identifiers.max_candidates", 1)),
            minimum_ocr_score=_positive_number(
                identifiers["minimum_ocr_score"], "identifiers.minimum_ocr_score", 0
            ),
            same_line_gap=_positive_number(identifiers["same_line_gap"], "identifiers.same_line_gap", 0),
            next_line_gap=_positive_number(identifiers["next_line_gap"], "identifiers.next_line_gap", 0),
            nearby_distance=_positive_number(
                identifiers["nearby_distance"], "identifiers.nearby_distance", 0
            ),
            tie_confidence_delta=_positive_number(
                identifiers["tie_confidence_delta"], "identifiers.tie_confidence_delta", 0
            ),
            tie_distance_delta=_positive_number(
                identifiers["tie_distance_delta"], "identifiers.tie_distance_delta", 0
            ),
        ),
        profile=str(identifiers["profile"]).strip() or "edge-rk3588",
        preprocessing=PreprocessSettings(
            perspective_correction=bool(preprocessing["perspective_correction"]),
            min_document_area_ratio=_positive_number(
                preprocessing["min_document_area_ratio"], "preprocessing.min_document_area_ratio", 0
            ),
            min_confidence=_positive_number(
                preprocessing["min_confidence"], "preprocessing.min_confidence", 0
            ),
            min_output_side=int(
                _positive_number(preprocessing["min_output_side"], "preprocessing.min_output_side", 32)
            ),
        ),
        identifier_rules=parse_identifier_rule_settings(identifier_rules),
    )


def with_endpoint_overrides(
    settings: ParserSettings,
    ocr_endpoint: Optional[str] = None,
    llm_endpoint: Optional[str] = None,
) -> ParserSettings:
    if ocr_endpoint:
        settings = replace(settings, ocr=replace(settings.ocr, endpoint=_local_http_url(ocr_endpoint, "ocr.endpoint")))
    if llm_endpoint:
        settings = replace(settings, llm=replace(settings.llm, endpoint=_local_http_url(llm_endpoint, "llm.endpoint")))
    return settings
