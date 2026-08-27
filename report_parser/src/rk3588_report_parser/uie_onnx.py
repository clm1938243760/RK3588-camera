"""Lightweight ONNX Runtime adapter for text-only PaddleNLP UIE models."""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import numpy as np

from .uie_extraction import UieRuntimeError


@dataclass(frozen=True)
class _TokenPiece:
    token: str
    token_id: int
    start: int
    end: int


class ErnieWordPieceTokenizer:
    """Small ERNIE tokenizer implementing the UIE inference subset."""

    def __init__(self, vocab_path: Path, do_lower_case: bool = True) -> None:
        try:
            tokens = vocab_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise UieRuntimeError("failed to read UIE vocabulary") from exc
        self.vocab = {token: index for index, token in enumerate(tokens)}
        self.do_lower_case = bool(do_lower_case)
        for token in ("[PAD]", "[CLS]", "[SEP]", "[UNK]"):
            if token not in self.vocab:
                raise UieRuntimeError("UIE vocabulary is missing %s" % token)
        self.pad_id = self.vocab["[PAD]"]
        self.cls_id = self.vocab["[CLS]"]
        self.sep_id = self.vocab["[SEP]"]
        self.unk_id = self.vocab["[UNK]"]

    def tokenize_with_offsets(self, text: str) -> list[_TokenPiece]:
        normalized_text, char_mapping = self._normalized_text(text)
        split_tokens = []
        for token in self._basic_tokenize(text):
            split_tokens.extend(self._wordpiece_tokenize(token))
        if not split_tokens:
            return []

        raw_offsets: list[list[int]] = []
        cursor = 0
        for token in split_tokens:
            needle = token[2:] if token.startswith("##") else token
            position = normalized_text.find(needle, cursor) if needle != "[UNK]" else -1
            if position < 0:
                raw_offsets.append([-1, -1])
                continue
            end = position + len(needle)
            raw_offsets.append([position, end])
            cursor = end

        pieces = []
        for index, token in enumerate(split_tokens):
            start, end = raw_offsets[index]
            if start < 0:
                start = raw_offsets[index - 1][1] if index else 0
                end = self._next_known_start(raw_offsets, index + 1, len(char_mapping))
            if not char_mapping or start >= len(char_mapping):
                continue
            end = max(start + 1, min(end, len(char_mapping)))
            pieces.append(
                _TokenPiece(
                    token=token,
                    token_id=self.vocab.get(token, self.unk_id),
                    start=char_mapping[start],
                    end=char_mapping[end - 1] + 1,
                )
            )
        return pieces

    @staticmethod
    def _next_known_start(offsets: Sequence[Sequence[int]], start: int, default: int) -> int:
        for value in offsets[start:]:
            if value[0] >= 0:
                return int(value[0])
        return default

    def _normalized_text(self, text: str) -> tuple[str, list[int]]:
        normalized = []
        mapping = []
        for index, value in enumerate(text):
            chars = value.lower() if self.do_lower_case else value
            if self.do_lower_case:
                chars = "".join(
                    char
                    for char in unicodedata.normalize("NFD", chars)
                    if unicodedata.category(char) != "Mn"
                )
            chars = "".join(char for char in chars if not _is_invalid_control(char))
            normalized.append(chars)
            mapping.extend([index] * len(chars))
        return "".join(normalized), mapping

    def _basic_tokenize(self, text: str) -> list[str]:
        cleaned = []
        for value in text:
            if _is_invalid_control(value):
                continue
            cleaned.append(" " if _is_whitespace(value) else value)
        spaced = []
        for value in "".join(cleaned):
            if _is_chinese_char(ord(value)):
                spaced.extend((" ", value, " "))
            else:
                spaced.append(value)

        output = []
        for token in "".join(spaced).split():
            if self.do_lower_case:
                token = "".join(
                    char
                    for char in unicodedata.normalize("NFD", token.lower())
                    if unicodedata.category(char) != "Mn"
                )
            current = []
            for value in token:
                if _is_punctuation_or_symbol(value):
                    if current:
                        output.append("".join(current))
                        current = []
                    output.append(value)
                else:
                    current.append(value)
            if current:
                output.append("".join(current))
        return output

    def _wordpiece_tokenize(self, token: str) -> list[str]:
        if len(token) > 100:
            return ["[UNK]"]
        pieces = []
        start = 0
        while start < len(token):
            end = len(token)
            selected: Optional[str] = None
            while start < end:
                candidate = token[start:end]
                if start:
                    candidate = "##" + candidate
                if candidate in self.vocab:
                    selected = candidate
                    break
                end -= 1
            if selected is None:
                return ["[UNK]"]
            pieces.append(selected)
            start = end
        return pieces


class OnnxUieEngine:
    """UIE text extraction backed by an ONNX Runtime ARM/desktop session."""

    def __init__(
        self,
        model_path: Path,
        vocab_path: Path,
        prompts: Sequence[str],
        *,
        position_prob: float = 0.5,
        max_seq_len: int = 512,
        intra_op_threads: int = 4,
    ) -> None:
        if not 0.0 < position_prob < 1.0:
            raise ValueError("UIE position probability must be between zero and one")
        if max_seq_len < 16 or max_seq_len > 512:
            raise ValueError("UIE max sequence length must be in range 16..512")
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise UieRuntimeError("onnxruntime is not installed") from exc
        if not model_path.is_file():
            raise UieRuntimeError("UIE ONNX model does not exist")

        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, int(intra_op_threads))
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.enable_cpu_mem_arena = True
        options.enable_mem_pattern = True
        try:
            self.session = ort.InferenceSession(
                str(model_path),
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
        except Exception as exc:
            raise UieRuntimeError("failed to initialize UIE ONNX model") from exc
        self.tokenizer = ErnieWordPieceTokenizer(vocab_path)
        self.position_prob = float(position_prob)
        self.max_seq_len = int(max_seq_len)
        self.set_prompts(prompts)
        self.runtime_info = {
            "inference_backend": "onnxruntime_cpu",
            "model_format": "onnx",
            "model_file": model_path.name,
            "quantized": any(
                marker in model_path.name.lower() for marker in (".int8.", ".uint8.")
            ),
            "intra_op_threads": options.intra_op_num_threads,
        }

    def set_prompts(self, prompts: Sequence[str]) -> None:
        normalized = [str(value).strip() for value in prompts if str(value).strip()]
        if not normalized:
            raise ValueError("UIE prompts must not be empty")
        self.prompts = tuple(normalized)

    def predict(self, text: str) -> Mapping[str, Any]:
        if not isinstance(text, str):
            raise ValueError("UIE input text must be a string")
        try:
            text_pieces = self.tokenizer.tokenize_with_offsets(text)
            return {
                prompt: self._predict_prompt(prompt, text, text_pieces)
                for prompt in self.prompts
            }
        except UieRuntimeError:
            raise
        except Exception as exc:
            raise UieRuntimeError("UIE ONNX inference failed") from exc

    def _predict_prompt(
        self,
        prompt: str,
        text: str,
        text_pieces: Sequence[_TokenPiece],
    ) -> list[dict[str, Any]]:
        prompt_pieces = self.tokenizer.tokenize_with_offsets(prompt)
        capacity = self.max_seq_len - len(prompt_pieces) - 3
        if capacity < 1:
            raise UieRuntimeError("UIE prompt is too long")
        if not text_pieces:
            return []

        predictions: dict[tuple[int, int], dict[str, Any]] = {}
        for chunk_start in range(0, len(text_pieces), capacity):
            chunk = text_pieces[chunk_start : chunk_start + capacity]
            for result in self._infer_chunk(prompt_pieces, chunk, text):
                key = (result["start"], result["end"])
                previous = predictions.get(key)
                if previous is None or result["probability"] > previous["probability"]:
                    predictions[key] = result
        return sorted(
            predictions.values(),
            key=lambda value: (value["start"], value["end"], -value["probability"]),
        )

    def _infer_chunk(
        self,
        prompt_pieces: Sequence[_TokenPiece],
        text_pieces: Sequence[_TokenPiece],
        source_text: str,
    ) -> list[dict[str, Any]]:
        input_ids = [self.tokenizer.cls_id]
        input_ids.extend(value.token_id for value in prompt_pieces)
        input_ids.append(self.tokenizer.sep_id)
        text_start = len(input_ids)
        input_ids.extend(value.token_id for value in text_pieces)
        input_ids.append(self.tokenizer.sep_id)
        sequence_length = len(input_ids)
        token_type_ids = [0] * text_start + [1] * (sequence_length - text_start)
        inputs = {
            "input_ids": np.asarray([input_ids], dtype=np.int64),
            "token_type_ids": np.asarray([token_type_ids], dtype=np.int64),
            "position_ids": np.asarray([list(range(sequence_length))], dtype=np.int64),
            "attention_mask": np.ones((1, sequence_length), dtype=np.int64),
        }
        try:
            start_prob, end_prob = self.session.run(None, inputs)
        except Exception as exc:
            raise UieRuntimeError("UIE ONNX inference failed") from exc

        starts = [
            (index, float(probability))
            for index, probability in enumerate(start_prob[0])
            if float(probability) > self.position_prob
        ]
        ends = [
            (index, float(probability))
            for index, probability in enumerate(end_prob[0])
            if float(probability) > self.position_prob
        ]
        spans = _pair_spans(starts, ends)
        results = []
        text_end = text_start + len(text_pieces)
        for start, end in spans:
            if start[0] < text_start or end[0] >= text_end:
                continue
            first = text_pieces[start[0] - text_start]
            last = text_pieces[end[0] - text_start]
            if first.start >= last.end:
                continue
            probability = start[1] * end[1]
            if not math.isfinite(probability):
                continue
            results.append(
                {
                    "text": source_text[first.start : last.end],
                    "start": first.start,
                    "end": last.end,
                    "probability": probability,
                }
            )
        return results


def _pair_spans(
    starts: Sequence[tuple[int, float]],
    ends: Sequence[tuple[int, float]],
) -> list[tuple[tuple[int, float], tuple[int, float]]]:
    start_values = sorted(starts, key=lambda value: value[0])
    end_values = sorted(ends, key=lambda value: value[0])
    start_index = 0
    end_index = 0
    couples: dict[tuple[int, float], tuple[int, float]] = {}
    while start_index < len(start_values) and end_index < len(end_values):
        start = start_values[start_index]
        end = end_values[end_index]
        if start[0] == end[0]:
            couples[end] = start
            start_index += 1
            end_index += 1
        elif start[0] < end[0]:
            couples[end] = start
            start_index += 1
        else:
            end_index += 1
    return sorted(((start, end) for end, start in couples.items()), key=lambda value: value[0][0])


def _is_whitespace(value: str) -> bool:
    return value in {" ", "\t", "\n", "\r"} or unicodedata.category(value) == "Zs"


def _is_invalid_control(value: str) -> bool:
    if value in {"\t", "\n", "\r"}:
        return False
    codepoint = ord(value)
    return codepoint in {0, 0xFFFD} or unicodedata.category(value) in {"Cc", "Cf"}


def _is_punctuation_or_symbol(value: str) -> bool:
    codepoint = ord(value)
    if 33 <= codepoint <= 47 or 58 <= codepoint <= 64 or 91 <= codepoint <= 96 or 123 <= codepoint <= 126:
        return True
    return unicodedata.category(value).startswith(("P", "S"))


def _is_chinese_char(codepoint: int) -> bool:
    return (
        0x4E00 <= codepoint <= 0x9FFF
        or 0x3400 <= codepoint <= 0x4DBF
        or 0x20000 <= codepoint <= 0x2A6DF
        or 0x2A700 <= codepoint <= 0x2B73F
        or 0x2B740 <= codepoint <= 0x2B81F
        or 0x2B820 <= codepoint <= 0x2CEAF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x2F800 <= codepoint <= 0x2FA1F
    )
