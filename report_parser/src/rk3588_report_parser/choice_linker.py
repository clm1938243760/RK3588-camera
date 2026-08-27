from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional, Sequence

from .clients import LocalSpanChoiceClient, SpanChoiceClientProtocol
from .models import FIELD_NAMES
from .prompt import FIELD_GUIDE, ModelResponseError
from .settings import LlmSettings
from .validation import LABEL_TEXTS, is_plausible_field_value


CONSTRAINED_CHOICE_SYSTEM_PROMPT = """You associate one medical-report field with OCR evidence.
You cannot inspect the original image and must not invent any text.
For the requested field, choose exactly one decimal candidate ID. Choose 0 when
there is no reliable value. Never choose a field label as its value. Do not
guess from formatting, and do not explain the answer."""


def _compact_label(value: str) -> str:
    return re.sub(r"[\s:\-\uFF1A]+", "", value or "").lower()


_GENERIC_LABELS = frozenset(_compact_label(value) for value in LABEL_TEXTS)

# Pick distinct, high-signal fields before the optional HIS/check identifier.
# Otherwise a report number can be consumed by the optional field and leave
# the later report fields with no valid candidate.  This is field semantics,
# not a report-layout template.
SELECTION_ORDER = (
    "patient_name",
    "patient_id",
    "sex",
    "age",
    "birthday",
    "report_no",
    "report_date",
    "exam_item",
    "his_exam_no",
)

def _is_generic_label(span: Dict[str, Any]) -> bool:
    return _compact_label(str(span["text"])) in _GENERIC_LABELS


class ConstrainedChoiceLinker:
    """Desktop benchmark linker with one-token constrained selections.

    This is intentionally a PC-only experiment.  It tests semantic selection
    independently from free-form JSON compliance and keeps the parser's final
    values traceable to original OCR span IDs.
    """

    def __init__(
        self,
        choice_client: Optional[SpanChoiceClientProtocol] = None,
        max_candidate_spans: int = 127,
        target_fields: Optional[Sequence[str]] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        if max_candidate_spans < 1 or max_candidate_spans > 127:
            raise ValueError("max_candidate_spans must be from 1 to 127")
        self.choice_client = choice_client or LocalSpanChoiceClient()
        self.max_candidate_spans = max_candidate_spans
        requested = tuple(target_fields) if target_fields is not None else SELECTION_ORDER
        unknown = set(requested) - set(FIELD_NAMES)
        if unknown or not requested:
            raise ValueError("target_fields must contain supported fields")
        self.selection_order = tuple(field for field in SELECTION_ORDER if field in requested)
        self.progress_callback = progress_callback

    def link(self, system_prompt: str, user_prompt: str, settings: LlmSettings) -> str:
        del system_prompt  # A narrower fixed prompt prevents output-schema drift.
        payload = self._prompt_payload(user_prompt)
        spans = self._normalize_spans(payload)
        if len(spans) > self.max_candidate_spans:
            raise ModelResponseError(
                "constrained choice has %d OCR spans; maximum is %d"
                % (len(spans), self.max_candidate_spans)
            )

        # Generic labels remain visible as context, but can never be emitted
        # as a value.  This is a content-level safety rule, not a hospital
        # template or a coordinate rule; combined label/value OCR spans stay
        # selectable and are normalized by the existing validator.
        remaining = {span["id"]: span for span in spans if not _is_generic_label(span)}
        links = self._fixed_links(payload, remaining)
        for span_ids in links.values():
            for span_id in span_ids:
                remaining.pop(span_id, None)

        total_fields = len(self.selection_order)
        for index, field in enumerate(self.selection_order, start=1):
            if links[field]:
                self._progress("field linking %d/%d: %s uses label evidence" % (index, total_fields, field))
                continue
            candidates = [
                remaining[span_id]
                for span_id in sorted(remaining)
                if is_plausible_field_value(field, str(remaining[span_id]["text"]))
            ]
            if not candidates:
                self._progress("field linking %d/%d: %s has no plausible candidate" % (index, total_fields, field))
                continue
            self._progress("field linking %d/%d: %s" % (index, total_fields, field))
            prompt = {
                "task": "choose one OCR span ID for this field, or 0 for no reliable value",
                "field": field,
                "field_meaning": FIELD_GUIDE[field],
                "rules": [
                    "Select a value, never a field label.",
                    "Choose 0 unless the evidence is reliable.",
                    "The selected ID must be one of the candidates.",
                ],
                "ocr_context": spans,
                "selectable_candidates": [{"id": 0, "text": "NO_RELIABLE_VALUE"}] + candidates,
            }
            allowed_ids = [0] + [span["id"] for span in candidates]
            choice_id = self.choice_client.select(
                CONSTRAINED_CHOICE_SYSTEM_PROMPT,
                json.dumps(prompt, ensure_ascii=False, separators=(",", ":")),
                settings,
                allowed_ids,
            )
            if choice_id not in allowed_ids:
                raise ModelResponseError("constrained choice returned an unavailable span ID")
            if choice_id:
                links[field] = [choice_id]
                # A source span cannot support two independent fields.  The
                # final validator enforces this too; removing it here makes
                # each next model decision unambiguous and fail-closed.
                remaining.pop(choice_id, None)

        return json.dumps(links, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _prompt_payload(user_prompt: str) -> Dict[str, Any]:
        try:
            payload = json.loads(user_prompt)
        except json.JSONDecodeError as exc:
            raise ModelResponseError("constrained choice input is not JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("spans"), list):
            raise ModelResponseError("constrained choice input has no spans")
        return payload

    @staticmethod
    def _normalize_spans(payload: Dict[str, Any]) -> List[Dict[str, Any]]:

        normalized: List[Dict[str, Any]] = []
        seen_ids = set()
        for raw_span in payload["spans"]:
            if not isinstance(raw_span, dict):
                raise ModelResponseError("constrained choice span must be an object")
            span_id = raw_span.get("id")
            text = raw_span.get("text")
            if (
                not isinstance(span_id, int)
                or isinstance(span_id, bool)
                or span_id < 1
                or span_id in seen_ids
                or not isinstance(text, str)
                or not text.strip()
            ):
                raise ModelResponseError("constrained choice span is invalid")
            seen_ids.add(span_id)
            candidate: Dict[str, Any] = {"id": span_id, "text": text}
            for key in ("line", "box", "score"):
                if key in raw_span:
                    candidate[key] = raw_span[key]
            normalized.append(candidate)
        if not normalized:
            raise ModelResponseError("constrained choice input has no usable spans")
        return normalized

    @staticmethod
    def _fixed_links(payload: Dict[str, Any], selectable_spans: Dict[int, Dict[str, Any]]) -> Dict[str, List[int]]:
        raw_fixed = payload.get("fixed_links", {})
        if raw_fixed is None:
            raw_fixed = {}
        if not isinstance(raw_fixed, dict):
            raise ModelResponseError("constrained choice fixed_links must be an object")

        links: Dict[str, List[int]] = {field: [] for field in FIELD_NAMES}
        used_ids = set()
        for field in FIELD_NAMES:
            raw_ids = raw_fixed.get(field, [])
            if not isinstance(raw_ids, list) or len(raw_ids) > 1:
                raise ModelResponseError("constrained choice fixed_links field is invalid")
            if not raw_ids:
                continue
            span_id = raw_ids[0]
            if (
                not isinstance(span_id, int)
                or isinstance(span_id, bool)
                or span_id not in selectable_spans
                or span_id in used_ids
            ):
                raise ModelResponseError("constrained choice fixed_links span is invalid")
            links[field] = [span_id]
            used_ids.add(span_id)
        return links

    def _progress(self, message: str) -> None:
        if self.progress_callback is not None:
            self.progress_callback(message)
