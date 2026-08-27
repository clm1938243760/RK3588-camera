"""UIE text extraction with strict OCR evidence tracing."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence


STANDARD_PATIENT_FIELDS = (
    "birthday",
    "exam_item",
    "ming",
    "sex",
    "yue",
    "his_exam_no",
    "xing",
    "patient_id",
    "ri",
    "patient_name",
    "name_phonetic",
    "nian",
    "report_no",
    "age",
)

DEFAULT_UIE_FIELDS = (
    {"field_key": "patient_name", "prompt": "患者姓名", "required": False},
    {"field_key": "patient_id", "prompt": "患者ID", "required": False},
    {
        "field_key": "his_exam_no",
        "prompt": "检查号或申请单号",
        "prompt_aliases": ["申请单号", "处方/申请号", "检查申请号"],
        "required": False,
    },
    {"field_key": "report_no", "prompt": "报告号", "prompt_aliases": ["报告单号"], "required": False},
    {"field_key": "exam_item", "prompt": "检查项目", "prompt_aliases": ["项目名称"], "required": False},
    {"field_key": "sex", "prompt": "患者性别", "required": False},
    {"field_key": "age", "prompt": "患者年龄", "required": False},
    {"field_key": "birthday", "prompt": "出生日期", "required": False},
    {"field_key": "name_phonetic", "prompt": "患者姓名拼音", "required": False},
)

CAPTURE_BLOCK_LIMIT = 4096
COMPOUND_SURNAMES = (
    "欧阳", "太史", "端木", "上官", "司马", "东方", "独孤", "南宫",
    "万俟", "闻人", "夏侯", "诸葛", "尉迟", "公羊", "赫连", "澹台",
    "皇甫", "宗政", "濮阳", "公冶", "太叔", "申屠", "公孙", "慕容",
    "仲孙", "钟离", "长孙", "宇文", "司徒", "鲜于", "司空", "闾丘",
    "子车", "亓官", "司寇", "巫马", "公西", "颛孙", "壤驷", "公良",
    "漆雕", "乐正", "宰父", "谷梁", "拓跋", "夹谷", "轩辕", "令狐",
    "段干", "百里", "呼延", "东郭", "南门", "羊舌", "微生", "梁丘",
    "左丘", "东门", "西门", "第五",
)

IDENTIFIER_FIELDS = {"patient_id", "his_exam_no", "report_no"}
NAME_FIELDS = {"patient_name", "xing", "ming"}
FIELD_LABEL_HINTS = {
    "patient_name": ("患者姓名", "姓名"),
    "patient_id": ("患者ID", "病人ID", "就诊卡号", "卡号", "ID号"),
    "his_exam_no": ("处方/申请号", "检查申请号", "申请单号", "申请号", "检查号"),
    "report_no": ("报告单号", "报告号"),
    "exam_item": ("检查项目", "项目名称", "检查名称"),
    "sex": ("患者性别", "性别"),
    "age": ("患者年龄", "年龄"),
    "birthday": ("出生日期", "出生年月"),
    "name_phonetic": ("患者姓名拼音", "姓名拼音"),
}
IDENTIFIER_TOKEN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9._/-]{2,62}[A-Za-z0-9])?"
)
DATE_TOKEN = re.compile(
    r"(?<!\d)(\d{4})\s*(?:[-/.年])\s*(\d{1,2})\s*"
    r"(?:[-/.月])\s*(\d{1,2})\s*日?"
)
AGE_TOKEN = re.compile(r"(?<!\d)(\d{1,3})\s*(?:周岁|岁)(?!\d)")
SEX_TOKEN = re.compile(r"男性|女性|男|女")


@dataclass(frozen=True)
class TextSegment:
    span_id: int
    start: int
    end: int
    text: str
    score: float
    box: tuple[int, int, int, int]
    normalized_box: tuple[int, int, int, int]


@dataclass(frozen=True)
class EvidenceDocument:
    text: str
    segments: tuple[TextSegment, ...]


class UieRuntimeError(RuntimeError):
    pass


class PaddleTaskflowEngine:
    """Lazy optional PaddleNLP dependency used only by the desktop experiment."""

    def __init__(
        self,
        model: str,
        prompts: Sequence[str],
        device: str = "cpu",
        position_prob: float = 0.5,
        max_seq_len: int = 512,
    ) -> None:
        try:
            from paddlenlp import Taskflow
        except ImportError as exc:
            raise UieRuntimeError(
                "PaddleNLP is not installed; use the isolated .venv-uie environment"
            ) from exc
        if device not in {"cpu", "gpu"}:
            raise ValueError("UIE device must be cpu or gpu")
        try:
            self.pipeline = Taskflow(
                "information_extraction",
                schema=list(prompts),
                model=model,
                device_id=-1 if device == "cpu" else 0,
                position_prob=position_prob,
                max_seq_len=max_seq_len,
                batch_size=1,
            )
        except Exception as exc:
            raise UieRuntimeError("failed to initialize UIE model: %s" % model) from exc

    def predict(self, text: str) -> Mapping[str, Any]:
        try:
            result = self.pipeline(text)
        except Exception as exc:
            raise UieRuntimeError("UIE inference failed") from exc
        if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict):
            raise UieRuntimeError("UIE returned an unexpected response")
        return result[0]

    def set_prompts(self, prompts: Sequence[str]) -> None:
        normalized = [str(value).strip() for value in prompts if str(value).strip()]
        if not normalized:
            raise ValueError("UIE prompts must not be empty")
        try:
            self.pipeline.set_schema(normalized)
        except Exception as exc:
            raise UieRuntimeError("failed to update UIE schema") from exc


class PaddleTaskflowXEngine:
    """UIE-X adapter that consumes the report image and existing OCR layout."""

    def __init__(
        self,
        prompts: Sequence[str],
        device: str = "cpu",
        position_prob: float = 0.5,
        max_seq_len: int = 512,
    ) -> None:
        try:
            from paddlenlp import Taskflow
        except ImportError as exc:
            raise UieRuntimeError(
                "PaddleNLP is not installed; use the isolated .venv-uie environment"
            ) from exc
        if device not in {"cpu", "gpu"}:
            raise ValueError("UIE-X device must be cpu or gpu")
        try:
            self.pipeline = Taskflow(
                "information_extraction",
                schema=list(prompts),
                model="uie-x-base",
                device_id=-1 if device == "cpu" else 0,
                position_prob=position_prob,
                max_seq_len=max_seq_len,
                batch_size=1,
            )
        except Exception as exc:
            raise UieRuntimeError("failed to initialize UIE-X model") from exc

    def predict(
        self,
        image_path: Path,
        layout: Sequence[Sequence[Any]],
    ) -> Mapping[str, Any]:
        if not image_path.is_file():
            raise ValueError("UIE-X image does not exist")
        prepared_path: Optional[Path] = None
        try:
            prepared_path, prepared_layout = _prepare_uie_x_document(image_path, layout)
            result = self.pipeline({"doc": str(prepared_path), "layout": prepared_layout})
        except Exception as exc:
            raise UieRuntimeError("UIE-X inference failed") from exc
        finally:
            if prepared_path is not None and prepared_path != image_path:
                try:
                    prepared_path.unlink()
                except FileNotFoundError:
                    pass
        if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict):
            raise UieRuntimeError("UIE-X returned an unexpected response")
        return result[0]


def load_uie_schema(path: Optional[Path]) -> list[dict[str, Any]]:
    if path is None:
        return normalize_uie_schema({"fields": list(DEFAULT_UIE_FIELDS)})
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    return normalize_uie_schema(payload)


def normalize_uie_schema(payload: Any) -> list[dict[str, Any]]:
    fields = payload.get("fields") if isinstance(payload, dict) else None
    if not isinstance(fields, list) or not fields:
        raise ValueError("UIE schema must contain a non-empty fields array")
    normalized = []
    keys = set()
    prompts = set()
    prompt_count = 0
    for value in fields:
        if not isinstance(value, dict):
            raise ValueError("each UIE field must be an object")
        key = str(value.get("field_key", "")).strip()
        prompt = str(value.get("prompt", "")).strip()
        if not key or not prompt:
            raise ValueError("each UIE field requires field_key and prompt")
        if key not in STANDARD_PATIENT_FIELDS:
            raise ValueError("unsupported UIE patient field: %s" % key)
        raw_aliases = value.get("prompt_aliases", [])
        if isinstance(raw_aliases, str):
            raw_aliases = [part.strip() for part in re.split(r"[,，;；\n]", raw_aliases)]
        if not isinstance(raw_aliases, (list, tuple)):
            raise ValueError("UIE prompt_aliases must be an array")
        aliases = []
        for alias_value in raw_aliases:
            alias = str(alias_value).strip()
            if not alias or alias == prompt or alias in aliases:
                continue
            if len(alias) > 80:
                raise ValueError("UIE prompts must contain at most 80 characters")
            aliases.append(alias)
        if len(prompt) > 80 or len(aliases) > 8:
            raise ValueError("each UIE field supports at most eight prompt aliases")
        field_prompts = [prompt, *aliases]
        if key in keys or any(value in prompts for value in field_prompts):
            raise ValueError("UIE field keys and prompts must be unique")
        keys.add(key)
        prompts.update(field_prompts)
        prompt_count += len(field_prompts)
        if prompt_count > 32:
            raise ValueError("UIE schema supports at most 32 prompts")
        normalized.append({
            "field_key": key,
            "prompt": prompt,
            "prompt_aliases": aliases,
            "required": bool(value.get("required", False)),
            "minimum_probability": _bounded_probability(
                value.get("minimum_probability", 0.5)
            ),
        })
    return normalized


def build_evidence_document(blocks: Sequence[Mapping[str, Any]]) -> EvidenceDocument:
    prepared = _prepared_blocks(blocks)
    text_parts: list[str] = []
    segments: list[TextSegment] = []
    previous_line: Optional[int] = None
    cursor = 0
    for line_id, _, span_id, _, text, score, box, normalized_box in prepared:
        if text_parts:
            separator = "\n" if previous_line != line_id else " "
            text_parts.append(separator)
            cursor += len(separator)
        start = cursor
        text_parts.append(text)
        cursor += len(text)
        segments.append(TextSegment(
            span_id=span_id,
            start=start,
            end=cursor,
            text=text,
            score=score,
            box=box,
            normalized_box=normalized_box,
        ))
        previous_line = line_id
    return EvidenceDocument(text="".join(text_parts), segments=tuple(segments))


def build_layout_evidence_document(
    blocks: Sequence[Mapping[str, Any]],
) -> tuple[EvidenceDocument, list[list[Any]]]:
    """Build the separator-free text and layout expected by PaddleNLP UIE-X."""

    text_parts: list[str] = []
    segments: list[TextSegment] = []
    layout: list[list[Any]] = []
    cursor = 0
    for _, _, span_id, _, text, score, box, normalized_box in _prepared_blocks(blocks):
        start = cursor
        text_parts.append(text)
        cursor += len(text)
        segments.append(TextSegment(
            span_id=span_id,
            start=start,
            end=cursor,
            text=text,
            score=score,
            box=box,
            normalized_box=normalized_box,
        ))
        layout.append([list(box), text])
    return EvidenceDocument(text="".join(text_parts), segments=tuple(segments)), layout


def extract_uie_fields(
    document: EvidenceDocument,
    schema: Sequence[Mapping[str, Any]],
    response: Mapping[str, Any],
    model: str,
    elapsed_ms: float = 0.0,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    missing: list[str] = []
    conflicts: list[str] = []
    review_fields: list[str] = []
    resolution_warnings: list[dict[str, Any]] = []
    rejected_predictions: list[dict[str, Any]] = []
    for definition in schema:
        key = str(definition["field_key"])
        accepted = []
        fallback_allowed = True
        for prompt in _field_prompts(definition):
            predictions = response.get(prompt, [])
            if not isinstance(predictions, list):
                fallback_allowed = False
                rejected_predictions.append({
                    "field_key": definition["field_key"],
                    "prompt": prompt,
                    "reason": "invalid_result_type",
                })
                continue
            for prediction in predictions:
                evidence, reason = _prediction_to_evidence(document, definition, prediction)
                if evidence is None:
                    if reason in {
                        "invalid_prediction",
                        "missing_offsets",
                        "offset_out_of_range",
                        "text_not_from_ocr",
                        "probability_below_threshold",
                        "no_source_span",
                    }:
                        fallback_allowed = False
                    rejected_predictions.append({
                        "field_key": definition["field_key"],
                        "prompt": prompt,
                        "reason": reason,
                    })
                else:
                    evidence, validation_reason = _refine_field_evidence(
                        document, key, evidence
                    )
                    if evidence is None:
                        rejected_predictions.append({
                            "field_key": key,
                            "prompt": prompt,
                            "reason": validation_reason,
                            "probability": _prediction_probability(prediction),
                        })
                        continue
                    evidence["matched_prompt"] = prompt
                    accepted.append(evidence)
        if not accepted and fallback_allowed:
            accepted.extend(_fallback_field_evidence(document, definition))
        accepted.sort(key=lambda item: (-item["probability"], item["start"], item["end"]))
        accepted = _deduplicate_evidence(accepted)
        if not accepted:
            if bool(definition.get("required", False)):
                missing.append(key)
            if any(
                item.get("field_key") == key
                and item.get("reason") != "probability_below_threshold"
                for item in rejected_predictions
            ):
                review_fields.append(key)
            continue
        selected = dict(accepted[0])
        selected["alternatives"] = accepted[1:]
        if accepted[1:] and accepted[1]["value"] != selected["value"]:
            conflicts.append(key)
        method = str(selected.get("resolution_method") or "uie")
        if method != "uie":
            resolution_warnings.append({"field_key": key, "method": method})
        if method == "typed_unique_fallback":
            review_fields.append(key)
        fields[key] = selected

    review_fields = list(dict.fromkeys(review_fields))
    if not fields:
        status = "rejected"
    elif missing or conflicts or review_fields:
        status = "review_required"
    else:
        status = "accepted"
    patient = _patient_record(fields)
    response_payload = _patient_response(status, patient)
    return {
        "schema_version": 1,
        "status": status,
        "model": model,
        "fields": fields,
        "missing_fields": missing,
        "conflict_fields": conflicts,
        "review_fields": review_fields,
        "resolution_warnings": resolution_warnings,
        "rejected_predictions": rejected_predictions,
        "patient_response": response_payload,
        "source": {
            "block_count": len(document.segments),
            "text_sha256": hashlib.sha256(document.text.encode("utf-8")).hexdigest(),
        },
        "timings": {"uie_ms": round(float(elapsed_ms), 2)},
    }


def run_uie_extraction(
    blocks: Sequence[Mapping[str, Any]],
    schema: Sequence[Mapping[str, Any]],
    model: str,
    predictor: Callable[[str], Mapping[str, Any]],
) -> dict[str, Any]:
    document = build_evidence_document(blocks)
    started = time.monotonic()
    response = predictor(document.text)
    elapsed_ms = (time.monotonic() - started) * 1000.0
    return extract_uie_fields(document, schema, response, model, elapsed_ms)


def uie_prompts(schema: Sequence[Mapping[str, Any]]) -> list[str]:
    return [prompt for definition in schema for prompt in _field_prompts(definition)]


def _field_prompts(definition: Mapping[str, Any]) -> list[str]:
    prompts = [str(definition["prompt"])]
    raw_aliases = definition.get("prompt_aliases", [])
    if isinstance(raw_aliases, (list, tuple)):
        prompts.extend(str(value) for value in raw_aliases)
    return prompts


def _deduplicate_evidence(values: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    deduplicated: list[dict[str, Any]] = []
    seen = set()
    for value in values:
        key = (str(value.get("value", "")), tuple(value.get("source_span_ids", [])))
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(dict(value))
    return deduplicated


def _prediction_probability(prediction: Any) -> float:
    if not isinstance(prediction, Mapping):
        return 0.0
    try:
        return round(max(0.0, min(1.0, float(prediction.get("probability", 0.0)))), 6)
    except (TypeError, ValueError):
        return 0.0


def run_uie_x_extraction(
    blocks: Sequence[Mapping[str, Any]],
    schema: Sequence[Mapping[str, Any]],
    image_path: Path,
    predictor: Callable[[Path, Sequence[Sequence[Any]]], Mapping[str, Any]],
) -> dict[str, Any]:
    document, layout = build_layout_evidence_document(blocks)
    started = time.monotonic()
    response = predictor(image_path, layout)
    elapsed_ms = (time.monotonic() - started) * 1000.0
    return extract_uie_fields(document, schema, response, "uie-x-base", elapsed_ms)


def blocks_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        result = []
        for index, item in enumerate(payload, start=1):
            if not isinstance(item, Mapping):
                continue
            value = dict(item)
            value.setdefault("id", index)
            value.setdefault("line_id", index)
            value.setdefault("normalized_box", value.get("box", [0, 0, 0, 0]))
            result.append(value)
        if not result:
            raise ValueError("input JSON does not contain OCR blocks")
        return result
    document = payload.get("document") if isinstance(payload, Mapping) else None
    blocks = document.get("blocks") if isinstance(document, Mapping) else None
    if isinstance(blocks, list):
        return [dict(item) for item in blocks if isinstance(item, Mapping)]
    raw = payload.get("ocr") if isinstance(payload, Mapping) else None
    if isinstance(raw, list):
        result = []
        for index, item in enumerate(raw, start=1):
            if not isinstance(item, Mapping):
                continue
            value = dict(item)
            value.setdefault("id", index)
            value.setdefault("line_id", index)
            value.setdefault("normalized_box", value.get("box", [0, 0, 0, 0]))
            result.append(value)
        return result
    nested = payload.get("ocr") if isinstance(payload, Mapping) else None
    if isinstance(nested, Mapping) and isinstance(nested.get("ocr"), list):
        return blocks_from_payload(nested)
    raise ValueError("input JSON does not contain OCR blocks")


def _prediction_to_evidence(
    document: EvidenceDocument,
    definition: Mapping[str, Any],
    prediction: Any,
) -> tuple[Optional[dict[str, Any]], str]:
    if not isinstance(prediction, Mapping):
        return None, "invalid_prediction"
    try:
        start = int(prediction["start"])
        end = int(prediction["end"])
        probability = float(prediction.get("probability", 0.0))
    except (KeyError, TypeError, ValueError):
        return None, "missing_offsets"
    if start < 0 or end <= start or end > len(document.text):
        return None, "offset_out_of_range"
    source_value = document.text[start:end]
    if str(prediction.get("text", "")) != source_value:
        return None, "text_not_from_ocr"
    minimum = _bounded_probability(definition.get("minimum_probability", 0.5))
    if probability < minimum:
        return None, "probability_below_threshold"
    evidence = _range_evidence(document, start, end, probability)
    if evidence is None:
        return None, "no_source_span"
    return evidence, ""


def _range_evidence(
    document: EvidenceDocument,
    start: int,
    end: int,
    probability: float,
) -> Optional[dict[str, Any]]:
    segments = [item for item in document.segments if item.start < end and item.end > start]
    if not segments:
        return None
    return {
        "value": document.text[start:end],
        "start": start,
        "end": end,
        "probability": round(max(0.0, min(1.0, float(probability))), 6),
        "source_span_ids": [item.span_id for item in segments],
        "ocr_confidence": round(min(item.score for item in segments), 6),
        "boxes": [list(item.box) for item in segments],
        "normalized_boxes": [list(item.normalized_box) for item in segments],
    }


def _refine_field_evidence(
    document: EvidenceDocument,
    field_key: str,
    evidence: Mapping[str, Any],
) -> tuple[Optional[dict[str, Any]], str]:
    try:
        start = int(evidence["start"])
        end = int(evidence["end"])
        probability = float(evidence["probability"])
    except (KeyError, TypeError, ValueError):
        return None, "invalid_evidence"
    refined_range, reason = _field_value_range(document.text, field_key, start, end)
    if refined_range is None:
        return None, reason
    refined = _range_evidence(document, refined_range[0], refined_range[1], probability)
    if refined is None:
        return None, "no_source_span"
    if refined_range != (start, end):
        refined["raw_value"] = document.text[start:end]
        refined["resolution_method"] = "uie_typed_refinement"
    else:
        refined["resolution_method"] = "uie"
    return refined, ""


def _field_value_range(
    text: str,
    field_key: str,
    start: int,
    end: int,
) -> tuple[Optional[tuple[int, int]], str]:
    start, end = _trim_range(text, start, end)
    if end <= start:
        return None, "empty_field_value"
    value = text[start:end]

    if field_key in IDENTIFIER_FIELDS:
        matches = [
            match
            for match in IDENTIFIER_TOKEN.finditer(value)
            if len(match.group(0)) >= 4 and not _is_date_identifier(match.group(0))
        ]
        if len(matches) != 1:
            return None, "ambiguous_identifier_value" if matches else "invalid_identifier_value"
        match = matches[0]
        return (start + match.start(), start + match.end()), ""

    if field_key == "sex":
        matches = list(SEX_TOKEN.finditer(value))
        normalized = {
            "男" if match.group(0) in {"男", "男性"} else "女"
            for match in matches
        }
        if len(normalized) != 1 or not matches:
            return None, "invalid_sex_value"
        selected = next(
            match
            for match in matches
            if ("男" if match.group(0) in {"男", "男性"} else "女") in normalized
        )
        return (start + selected.start(), start + selected.end()), ""

    if field_key == "age":
        matches = [
            match for match in AGE_TOKEN.finditer(value)
            if 0 <= int(match.group(1)) <= 130
        ]
        if not matches and re.fullmatch(r"\d{1,3}", value):
            if 0 <= int(value) <= 130:
                return (start, end), ""
        if len(matches) != 1:
            return None, "invalid_age_value"
        return (start + matches[0].start(), start + matches[0].end()), ""

    if field_key == "birthday":
        matches = [match for match in DATE_TOKEN.finditer(value) if _valid_date_match(match)]
        if len(matches) != 1:
            return None, "invalid_birthday_value"
        return (start + matches[0].start(), start + matches[0].end()), ""

    if field_key in NAME_FIELDS:
        if re.fullmatch(r"[\u3400-\u9fff·]{1,12}", value):
            minimum = 1 if field_key in {"xing", "ming"} else 2
            if len(value) >= minimum:
                return (start, end), ""
        if field_key == "patient_name" and re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,63}", value):
            return (start, end), ""
        return None, "invalid_patient_name"

    if field_key == "exam_item":
        prefix = re.match(r"\d{1,8}\s*(?=[\u3400-\u9fff])", value)
        if prefix is not None:
            start += prefix.end()
            value = text[start:end]
        if not 2 <= len(value) <= 160 or not re.search(r"[\u3400-\u9fff]", value):
            return None, "invalid_exam_item"
        if value.strip(" ：:") in FIELD_LABEL_HINTS["exam_item"]:
            return None, "invalid_exam_item"
        return (start, end), ""

    if field_key == "name_phonetic":
        if re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,127}", value):
            return (start, end), ""
        return None, "invalid_name_phonetic"

    if field_key == "nian":
        if re.fullmatch(r"\d{4}", value) and 1900 <= int(value) <= 2100:
            return (start, end), ""
        return None, "invalid_year_value"
    if field_key in {"yue", "ri"}:
        if re.fullmatch(r"\d{1,2}", value):
            maximum = 12 if field_key == "yue" else 31
            if 1 <= int(value) <= maximum:
                return (start, end), ""
        return None, "invalid_date_part"
    return (start, end), ""


def _trim_range(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _is_date_identifier(value: str) -> bool:
    return bool(re.fullmatch(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}", value))


def _valid_date_match(match: re.Match[str]) -> bool:
    try:
        date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return False
    return True


def _fallback_field_evidence(
    document: EvidenceDocument,
    definition: Mapping[str, Any],
) -> list[dict[str, Any]]:
    field_key = str(definition["field_key"])
    candidates = _label_fallback_candidates(document, definition, field_key)
    if not candidates and field_key in {"sex", "age", "birthday"}:
        candidates = _typed_unique_candidates(document, field_key)
    return _deduplicate_evidence(candidates)


def _label_fallback_candidates(
    document: EvidenceDocument,
    definition: Mapping[str, Any],
    field_key: str,
) -> list[dict[str, Any]]:
    labels = []
    for value in [*_field_prompts(definition), *FIELD_LABEL_HINTS.get(field_key, ())]:
        label = str(value).strip().strip("：:")
        if label and label not in labels:
            labels.append(label)
    candidates: list[dict[str, Any]] = []
    segments = list(document.segments)
    for index, segment in enumerate(segments):
        for label in labels:
            position = segment.text.find(label)
            if position < 0 or segment.text[:position].strip(" \t：:-"):
                continue
            local_start = position + len(label)
            while local_start < len(segment.text) and segment.text[local_start] in " \t：:-":
                local_start += 1
            if local_start < len(segment.text):
                evidence = _fallback_range_evidence(
                    document,
                    field_key,
                    segment.start + local_start,
                    segment.end,
                    segment.score,
                    "label_inline_fallback",
                    label,
                    [segment.span_id],
                )
                if evidence is not None:
                    candidates.append(evidence)
                continue
            normalized_label = segment.text.strip().strip("：:")
            if normalized_label != label:
                continue
            for value_segment in segments[index + 1:index + 4]:
                if not _nearby_segment(segment, value_segment):
                    continue
                relation_score = min(segment.score, value_segment.score) * 0.95
                evidence = _fallback_range_evidence(
                    document,
                    field_key,
                    value_segment.start,
                    value_segment.end,
                    relation_score,
                    "label_neighbor_fallback",
                    label,
                    [segment.span_id],
                )
                if evidence is not None:
                    candidates.append(evidence)
                    break
    return candidates


def _fallback_range_evidence(
    document: EvidenceDocument,
    field_key: str,
    start: int,
    end: int,
    probability: float,
    method: str,
    matched_prompt: str,
    label_span_ids: Sequence[int],
) -> Optional[dict[str, Any]]:
    refined_range, _ = _field_value_range(document.text, field_key, start, end)
    if refined_range is None:
        return None
    evidence = _range_evidence(document, refined_range[0], refined_range[1], probability)
    if evidence is None:
        return None
    evidence["resolution_method"] = method
    evidence["matched_prompt"] = matched_prompt
    evidence["label_span_ids"] = list(label_span_ids)
    if refined_range != (start, end):
        evidence["raw_value"] = document.text[start:end]
    return evidence


def _nearby_segment(label: TextSegment, value: TextSegment) -> bool:
    vertical_gap = max(0, value.normalized_box[1] - label.normalized_box[3])
    return (
        value.normalized_box[1] >= label.normalized_box[1] - 35
        and vertical_gap <= 90
    )


def _typed_unique_candidates(
    document: EvidenceDocument,
    field_key: str,
) -> list[dict[str, Any]]:
    maximum_length = {"sex": 12, "age": 16, "birthday": 32}[field_key]
    candidates = []
    for segment in document.segments:
        if len(segment.text) > maximum_length:
            continue
        refined_range, _ = _field_value_range(
            document.text, field_key, segment.start, segment.end
        )
        if refined_range is None:
            continue
        evidence = _range_evidence(
            document,
            refined_range[0],
            refined_range[1],
            segment.score * 0.85,
        )
        if evidence is None:
            continue
        evidence["resolution_method"] = "typed_unique_fallback"
        evidence["matched_prompt"] = "字段类型唯一候选"
        if refined_range != (segment.start, segment.end):
            evidence["raw_value"] = segment.text
        candidates.append(evidence)
    distinct = {str(value["value"]) for value in candidates}
    return candidates if len(distinct) == 1 else []


def _patient_record(fields: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    patient = {key: None for key in STANDARD_PATIENT_FIELDS}
    for key, evidence in fields.items():
        if key in patient:
            patient[key] = evidence.get("value")
    name = str(patient.get("patient_name") or "").strip()
    if name:
        surname_length = 2 if name.startswith(COMPOUND_SURNAMES) else 1
        patient["xing"] = name[:surname_length]
        patient["ming"] = name[surname_length:] or None
    age = str(patient.get("age") or "").strip()
    age_match = re.fullmatch(r"(\d{1,3})\s*(?:周岁|岁)?", age)
    if age_match and 0 <= int(age_match.group(1)) <= 130:
        patient["age"] = str(int(age_match.group(1)))
    sex = str(patient.get("sex") or "").strip()
    if sex in {"男", "男性"}:
        patient["sex"] = "男"
    elif sex in {"女", "女性"}:
        patient["sex"] = "女"
    birthday = str(patient.get("birthday") or "").strip()
    match = re.fullmatch(r"(\d{4})\s*(?:[-/.年])\s*(\d{1,2})\s*(?:[-/.月])\s*(\d{1,2})\s*日?", birthday)
    if match:
        year, month, day = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        try:
            parsed = date(year, month, day)
        except ValueError:
            pass
        else:
            patient["birthday"] = parsed.isoformat()
            patient["nian"] = str(year)
            patient["yue"] = str(month).zfill(2)
            patient["ri"] = str(day).zfill(2)
    return patient


def _patient_response(status: str, patient: Mapping[str, Any]) -> dict[str, Any]:
    if status == "accepted":
        return {"code": "SUCCESS", "data": [dict(patient)], "msg": "成功", "success": True}
    if status == "review_required":
        return {"code": "REVIEW_REQUIRED", "data": [dict(patient)], "msg": "患者信息需要复核", "success": False}
    return {"code": "FAIL", "data": [], "msg": "未识别到患者信息", "success": False}


def build_patient_response(
    fields: Mapping[str, Mapping[str, Any]], status: str
) -> dict[str, Any]:
    return _patient_response(status, _patient_record(fields))


def _box(value: Mapping[str, Any], key: str) -> tuple[int, int, int, int]:
    raw = value.get(key) or value.get("box") or [0, 0, 0, 0]
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return (0, 0, 0, 0)
    try:
        return tuple(int(round(float(item))) for item in raw)  # type: ignore[return-value]
    except (TypeError, ValueError):
        return (0, 0, 0, 0)


def _bounded_probability(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError("UIE probability must be numeric") from None
    if result < 0.0 or result > 1.0:
        raise ValueError("UIE probability must be in range 0..1")
    return result


def _prepared_blocks(
    blocks: Sequence[Mapping[str, Any]],
) -> list[tuple[int, int, int, int, str, float, tuple[int, int, int, int], tuple[int, int, int, int]]]:
    if len(blocks) > CAPTURE_BLOCK_LIMIT:
        raise ValueError("OCR document contains too many blocks")
    prepared = []
    for index, block in enumerate(blocks):
        if not isinstance(block, Mapping):
            continue
        text = str(block.get("text", "")).strip()
        if not text:
            continue
        prepared.append((
            int(block.get("line_id", index + 1)),
            _box(block, "box")[0],
            int(block.get("id", index + 1)),
            index,
            text,
            float(block.get("score", 0.0)),
            _box(block, "box"),
            _box(block, "normalized_box"),
        ))
    prepared.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    return prepared


def _prepare_uie_x_document(
    image_path: Path,
    layout: Sequence[Sequence[Any]],
) -> tuple[Path, list[list[Any]]]:
    """Pre-pad to A4 so PaddleNLP 2.8.1 does not misapply its X offset to Y."""

    from PIL import Image, ImageOps

    with Image.open(image_path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
    width, height = image.size
    offset_x = 0
    offset_y = 0
    ratio = height / float(width)
    if ratio >= 1.42:
        expanded_width = int(height / 1.414 - width)
        offset_x = max(0, int(expanded_width / 2))
    elif ratio <= 1.40:
        expanded_height = int(width * 1.414 - height)
        offset_y = max(0, int(expanded_height / 2))
    if offset_x == 0 and offset_y == 0:
        return image_path, [[list(item[0]), str(item[1])] for item in layout]

    canvas = Image.new(
        "RGB",
        (width + 2 * offset_x, height + 2 * offset_y),
        color="white",
    )
    canvas.paste(image, (offset_x, offset_y))
    descriptor, temporary_name = tempfile.mkstemp(prefix="rk3588-uie-x-", suffix=".png")
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        canvas.save(temporary, format="PNG")
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    transformed = []
    for item in layout:
        if len(item) < 2:
            continue
        box = item[0]
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            continue
        transformed.append([
            [
                int(box[0]) + offset_x,
                int(box[1]) + offset_y,
                int(box[2]) + offset_x,
                int(box[3]) + offset_y,
            ],
            str(item[1]),
        ])
    return temporary, transformed
