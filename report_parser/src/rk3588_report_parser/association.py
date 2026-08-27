from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

from .models import FIELD_NAMES, OcrSpan
from .validation import FIELD_LABEL_PREFIXES


def _compact(value: str) -> str:
    return re.sub(r"[\s:：\-]+", "", value or "").lower()


def _label_tail(text: str, field: str) -> Optional[str]:
    compact_text = _compact(text)
    for label in sorted(FIELD_LABEL_PREFIXES[field], key=len, reverse=True):
        compact_label = _compact(label)
        if compact_label and compact_text.startswith(compact_label):
            return compact_text[len(compact_label) :]
    return None


def _is_label_only(text: str) -> bool:
    return any(_label_tail(text, field) == "" for field in FIELD_NAMES)


def high_confidence_label_links(spans: Sequence[OcrSpan]) -> Dict[str, List[int]]:
    """Find unambiguous generic label/value pairs without field coordinates.

    A candidate is accepted only if it is either inside the label OCR span
    itself or the nearest non-label span to its right on the same OCR line.
    Repeated labels with different candidates stay ambiguous and are omitted.
    """

    by_line: Dict[int, List[OcrSpan]] = {}
    for span in spans:
        by_line.setdefault(span.line_id, []).append(span)
    lines = {
        line_id: sorted(line_spans, key=lambda span: (span.box[0], span.id))
        for line_id, line_spans in by_line.items()
    }
    candidates: Dict[str, List[Tuple[int, ...]]] = {field: [] for field in FIELD_NAMES}

    for line in lines.values():
        for index, span in enumerate(line):
            for field in FIELD_NAMES:
                tail = _label_tail(span.text, field)
                if tail is None:
                    continue
                if tail:
                    candidates[field].append((span.id,))
                    continue
                for right in line[index + 1 :]:
                    if _is_label_only(right.text):
                        continue
                    candidates[field].append((right.id,))
                    break

    links: Dict[str, List[int]] = {}
    for field, raw_candidates in candidates.items():
        unique = sorted(set(raw_candidates))
        if len(unique) == 1:
            links[field] = list(unique[0])
    return links


def merge_model_and_label_links(
    model_links: Dict[str, List[int]],
    label_links: Dict[str, List[int]],
) -> Tuple[Dict[str, List[int]], List[str]]:
    """Prefer unique generic label/value evidence over an LLM disagreement."""

    merged: Dict[str, List[int]] = {}
    label_fields: List[str] = []
    for field in FIELD_NAMES:
        if label_links.get(field):
            merged[field] = list(label_links[field])
            label_fields.append(field)
        else:
            merged[field] = list(model_links.get(field, []))
    return merged, label_fields
