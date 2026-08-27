"""RKNN Lite adapter for the fixed-shape text-only UIE encoder."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import numpy as np

from .uie_extraction import UieRuntimeError
from .uie_onnx import ErnieWordPieceTokenizer, _pair_spans


class RknnUieEngine:
    """Execute an RK3588 UIE model while preserving UIE text offsets."""

    def __init__(
        self,
        model_path: Path,
        vocab_path: Path,
        prompts: Sequence[str],
        *,
        position_prob: float = 0.5,
        sequence_length: int = 256,
    ) -> None:
        if not 0.0 < position_prob < 1.0:
            raise ValueError("UIE position probability must be between zero and one")
        if sequence_length not in {128, 256, 512}:
            raise ValueError("RKNN UIE sequence length must be 128, 256, or 512")
        if not model_path.is_file():
            raise UieRuntimeError("UIE RKNN model does not exist")
        try:
            from rknnlite.api import RKNNLite
        except ImportError as exc:
            raise UieRuntimeError("rknn-toolkit-lite2 is not installed") from exc

        self._rknn_lite = RKNNLite
        self.rknn = RKNNLite(verbose=False)
        try:
            result = self.rknn.load_rknn(str(model_path))
            if result != 0:
                raise UieRuntimeError("failed to load UIE RKNN model: %s" % result)
            result = self.rknn.init_runtime(core_mask=RKNNLite.NPU_CORE_0_1_2)
            if result != 0:
                raise UieRuntimeError("failed to initialize UIE RKNN runtime: %s" % result)
        except UieRuntimeError:
            self.rknn.release()
            raise
        except Exception as exc:
            self.rknn.release()
            raise UieRuntimeError("failed to initialize UIE RKNN runtime") from exc
        self.tokenizer = ErnieWordPieceTokenizer(vocab_path)
        self.position_prob = float(position_prob)
        self.sequence_length = int(sequence_length)
        self.set_prompts(prompts)
        self.runtime_info = {
            "inference_backend": "rknn_lite_npu",
            "model_format": "rknn",
            "model_file": model_path.name,
            "quantized": False,
            "sequence_length": self.sequence_length,
            "npu_cores": "0_1_2",
        }

    def close(self) -> None:
        rknn = getattr(self, "rknn", None)
        if rknn is not None:
            rknn.release()
            self.rknn = None

    def set_prompts(self, prompts: Sequence[str]) -> None:
        normalized = [str(value).strip() for value in prompts if str(value).strip()]
        if not normalized:
            raise ValueError("UIE prompts must not be empty")
        self.prompts = tuple(normalized)

    def predict(self, text: str) -> Mapping[str, Any]:
        if not isinstance(text, str):
            raise ValueError("UIE input text must be a string")
        if self.rknn is None:
            raise UieRuntimeError("UIE RKNN runtime is closed")
        try:
            text_pieces = self.tokenizer.tokenize_with_offsets(text)
            return {
                prompt: self._predict_prompt(prompt, text, text_pieces)
                for prompt in self.prompts
            }
        except UieRuntimeError:
            raise
        except Exception as exc:
            raise UieRuntimeError("UIE RKNN inference failed") from exc

    def _predict_prompt(self, prompt: str, text: str, text_pieces: Sequence[Any]) -> list[dict[str, Any]]:
        prompt_pieces = self.tokenizer.tokenize_with_offsets(prompt)
        capacity = self.sequence_length - len(prompt_pieces) - 3
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
        prompt_pieces: Sequence[Any],
        text_pieces: Sequence[Any],
        source_text: str,
    ) -> list[dict[str, Any]]:
        input_ids = [self.tokenizer.cls_id]
        input_ids.extend(value.token_id for value in prompt_pieces)
        input_ids.append(self.tokenizer.sep_id)
        text_start = len(input_ids)
        input_ids.extend(value.token_id for value in text_pieces)
        input_ids.append(self.tokenizer.sep_id)
        sequence_length = len(input_ids)
        if sequence_length > self.sequence_length:
            raise UieRuntimeError("UIE text chunk exceeds RKNN sequence length")
        token_type_ids = [0] * text_start + [1] * (sequence_length - text_start)
        pad_size = self.sequence_length - sequence_length
        inputs = [
            np.asarray([input_ids + [self.tokenizer.pad_id] * pad_size], dtype=np.int64),
            np.asarray([token_type_ids + [self.tokenizer.pad_id] * pad_size], dtype=np.int64),
            np.asarray([list(range(sequence_length)) + [0] * pad_size], dtype=np.int64),
            np.asarray([[1] * sequence_length + [0] * pad_size], dtype=np.int64),
        ]
        try:
            outputs = self.rknn.inference(inputs=inputs)
        except Exception as exc:
            raise UieRuntimeError("UIE RKNN inference failed") from exc
        if not isinstance(outputs, (list, tuple)) or len(outputs) != 2:
            raise UieRuntimeError("UIE RKNN returned unexpected outputs")
        start_prob = np.asarray(outputs[0]).reshape(-1)
        end_prob = np.asarray(outputs[1]).reshape(-1)
        if len(start_prob) < sequence_length or len(end_prob) < sequence_length:
            raise UieRuntimeError("UIE RKNN output length is invalid")
        starts = [
            (index, float(probability))
            for index, probability in enumerate(start_prob[:sequence_length])
            if float(probability) > self.position_prob
        ]
        ends = [
            (index, float(probability))
            for index, probability in enumerate(end_prob[:sequence_length])
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
