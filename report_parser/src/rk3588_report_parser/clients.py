from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

from .settings import LlmSettings, OcrSettings


class ServiceError(RuntimeError):
    pass


class OcrClientProtocol(Protocol):
    def recognize(self, image_bytes: bytes, settings: OcrSettings) -> Dict[str, Any]:
        ...


class FieldLinkerProtocol(Protocol):
    def link(self, system_prompt: str, user_prompt: str, settings: LlmSettings) -> str:
        ...


class SpanChoiceClientProtocol(Protocol):
    def select(
        self,
        system_prompt: str,
        user_prompt: str,
        settings: LlmSettings,
        allowed_ids: Sequence[int],
    ) -> int:
        ...


def _post_json(endpoint: str, payload: Dict[str, Any], timeout_seconds: float) -> Dict[str, Any]:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(
        endpoint,
        data=raw,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read()
            status = response.status
    except HTTPError as exc:
        body = exc.read()
        snippet = body[:300].decode("utf-8", "replace")
        raise ServiceError("local service HTTP %d: %s" % (exc.code, snippet)) from exc
    except URLError as exc:
        raise ServiceError("local service unavailable: %s" % exc.reason) from exc
    except OSError as exc:
        raise ServiceError("local service request failed: %s" % exc) from exc
    if status != 200:
        raise ServiceError("local service HTTP %d" % status)
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServiceError("local service did not return JSON") from exc
    if not isinstance(parsed, dict):
        raise ServiceError("local service JSON must be an object")
    return parsed


class LocalPpOcrClient:
    def recognize(self, image_bytes: bytes, settings: OcrSettings) -> Dict[str, Any]:
        response = _post_json(
            settings.endpoint,
            {"image_base64": base64.b64encode(image_bytes).decode("ascii")},
            settings.timeout_seconds,
        )
        if not response.get("ok"):
            raise ServiceError("PP-OCR returned ok=false")
        if not isinstance(response.get("ocr"), list):
            raise ServiceError("PP-OCR response is missing ocr items")
        return response


class LocalOpenAIChatClient:
    """Adapter for a loopback-only OpenAI-compatible chat service.

    RKLLM on the target board and the desktop Qwen test server intentionally
    share this wire protocol.  That keeps the prompt, model output contract,
    and validation path identical before and after RK3588 deployment.
    """

    def link(self, system_prompt: str, user_prompt: str, settings: LlmSettings) -> str:
        response = _post_json(
            settings.endpoint,
            {
                "model": settings.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0,
                "top_p": 1,
                "max_tokens": settings.max_tokens,
                "stream": False,
            },
            settings.timeout_seconds,
        )
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str) and content.strip():
                        return content
        for key in ("response", "text", "content"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value
        raise ServiceError("local chat response is missing content")


def _span_choice_endpoint(chat_endpoint: str) -> str:
    parsed = urlparse(chat_endpoint)
    suffix = "/v1/chat/completions"
    if not parsed.path.endswith(suffix):
        raise ServiceError("local chat endpoint must end with %s" % suffix)
    choice_path = parsed.path[: -len(suffix)] + "/v1/span-choice"
    return urlunparse(parsed._replace(path=choice_path))


class LocalSpanChoiceClient:
    """Desktop-only client for exact span-ID choice decoding.

    The target RK3588 runtime does not use this endpoint yet.  It exists to
    prove on the PC whether Qwen can make the semantic association when JSON
    formatting is no longer a variable.
    """

    def select(
        self,
        system_prompt: str,
        user_prompt: str,
        settings: LlmSettings,
        allowed_ids: Sequence[int],
    ) -> int:
        normalized: List[int] = []
        for value in allowed_ids:
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ServiceError("allowed span IDs must be non-negative integers")
            if value not in normalized:
                normalized.append(value)
        if not normalized:
            raise ServiceError("allowed span IDs must not be empty")
        if len(normalized) > 128:
            raise ServiceError("allowed span IDs must contain at most 128 values")

        response = _post_json(
            _span_choice_endpoint(settings.endpoint),
            {
                "model": settings.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "allowed_ids": normalized,
            },
            settings.timeout_seconds,
        )
        choice_id = response.get("choice_id")
        if (
            not isinstance(choice_id, int)
            or isinstance(choice_id, bool)
            or choice_id not in normalized
        ):
            raise ServiceError("local span-choice response is invalid")
        return choice_id


# Retain the original name for callers that imported the first prototype.
LocalRkllmChatClient = LocalOpenAIChatClient
