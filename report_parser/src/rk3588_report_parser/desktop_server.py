from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple, Type


class DesktopRuntimeError(RuntimeError):
    pass


class ChatEngineProtocol(Protocol):
    def generate(self, messages: Sequence[Dict[str, str]], max_tokens: int) -> str:
        ...


class ChoiceEngineProtocol(Protocol):
    def choose(self, messages: Sequence[Dict[str, str]], allowed_ids: Sequence[int]) -> int:
        ...


def _load_torch_and_transformers() -> Tuple[Any, Any, Any]:
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        raise DesktopRuntimeError(
            "Desktop model dependencies are missing. Use Python 3.10-3.12, "
            "install a CUDA-enabled PyTorch build, then install requirements-pc.txt."
        ) from exc
    return torch, AutoModelForCausalLM, AutoTokenizer


def _resolve_device(torch: Any, requested: str) -> str:
    value = requested.strip().lower()
    if value == "auto":
        return "cuda:0" if torch.cuda.is_available() else "cpu"
    if value == "cuda":
        value = "cuda:0"
    if value == "cpu":
        return value
    if value.startswith("cuda:"):
        if not torch.cuda.is_available():
            raise DesktopRuntimeError("CUDA was requested but PyTorch cannot see an NVIDIA GPU")
        try:
            index = int(value.split(":", 1)[1])
        except ValueError as exc:
            raise DesktopRuntimeError("CUDA device must look like cuda:0") from exc
        if index < 0 or index >= torch.cuda.device_count():
            raise DesktopRuntimeError("requested CUDA device is not available")
        return value
    raise DesktopRuntimeError("device must be auto, cpu, cuda, or cuda:N")


class TransformersChatEngine:
    """Loads a local Hugging Face Qwen model without any network fallback."""

    def __init__(self, model_path: Path, device: str = "auto") -> None:
        if not model_path.is_dir():
            raise DesktopRuntimeError("model path is not a local directory: %s" % model_path)
        torch, auto_model, auto_tokenizer = _load_torch_and_transformers()
        self._torch = torch
        self.device = _resolve_device(torch, device)
        dtype = torch.float16 if self.device.startswith("cuda") else torch.float32
        try:
            self.tokenizer = auto_tokenizer.from_pretrained(
                str(model_path),
                local_files_only=True,
                trust_remote_code=False,
            )
            self.model = auto_model.from_pretrained(
                str(model_path),
                local_files_only=True,
                trust_remote_code=False,
                dtype=dtype,
                low_cpu_mem_usage=True,
            )
        except OSError as exc:
            raise DesktopRuntimeError("could not load local model files: %s" % exc) from exc
        self.model.to(self.device)
        self.model.eval()
        self._lock = threading.Lock()

    def generate(self, messages: Sequence[Dict[str, str]], max_tokens: int) -> str:
        prompt = self.tokenizer.apply_chat_template(
            list(messages),
            tokenize=False,
            add_generation_prompt=True,
        )
        encoded = self.tokenizer([prompt], return_tensors="pt")
        encoded = {key: value.to(self.device) for key, value in encoded.items()}
        kwargs: Dict[str, Any] = {
            **encoded,
            "max_new_tokens": max_tokens,
            "do_sample": False,
            "use_cache": True,
        }
        if isinstance(self.tokenizer.eos_token_id, int):
            kwargs["pad_token_id"] = self.tokenizer.eos_token_id
        with self._lock, self._torch.inference_mode():
            generated = self.model.generate(**kwargs)
        input_length = encoded["input_ids"].shape[1]
        text = self.tokenizer.batch_decode(generated[:, input_length:], skip_special_tokens=True)[0].strip()
        if not text:
            raise DesktopRuntimeError("local model returned an empty response")
        return text

    def choose(self, messages: Sequence[Dict[str, str]], allowed_ids: Sequence[int]) -> int:
        """Choose one existing OCR span ID with token-level constrained decoding.

        The desktop comparison must establish whether the model can perform
        field association, not whether it can format JSON.  Generation is
        therefore constrained to one of the supplied decimal IDs (usually
        including ``0`` for "no reliable evidence") and a final EOS token.
        """

        normalized_ids: List[int] = []
        for value in allowed_ids:
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise DesktopRuntimeError("allowed_ids must contain non-negative integers")
            if value not in normalized_ids:
                normalized_ids.append(value)
        if not normalized_ids:
            raise DesktopRuntimeError("allowed_ids must not be empty")
        if len(normalized_ids) > 128:
            raise DesktopRuntimeError("allowed_ids must contain at most 128 values")

        eos_token_id = self.tokenizer.eos_token_id
        if not isinstance(eos_token_id, int) or isinstance(eos_token_id, bool) or eos_token_id < 0:
            raise DesktopRuntimeError("local tokenizer does not expose one EOS token")

        prompt = self.tokenizer.apply_chat_template(
            list(messages),
            tokenize=False,
            add_generation_prompt=True,
        )
        encoded = self.tokenizer([prompt], return_tensors="pt")
        encoded = {key: value.to(self.device) for key, value in encoded.items()}
        input_length = encoded["input_ids"].shape[1]

        # Qwen may start a terse answer with no separator, a space, or a
        # newline depending on its chat template.  Every permitted variant
        # still decodes to precisely one decimal candidate ID.
        trie: Dict[int, Dict[Any, Any]] = {}
        max_choice_tokens = 0
        for choice_id in normalized_ids:
            for prefix in ("", " ", "\n"):
                token_ids = self.tokenizer.encode(prefix + str(choice_id), add_special_tokens=False)
                if not token_ids:
                    continue
                node: Dict[Any, Any] = trie
                for token_id in token_ids:
                    node = node.setdefault(int(token_id), {})
                node[eos_token_id] = {}
                max_choice_tokens = max(max_choice_tokens, len(token_ids))
        if not trie or max_choice_tokens == 0:
            raise DesktopRuntimeError("could not tokenize allowed IDs")

        def prefix_allowed_tokens(_: int, generated_ids: Any) -> List[int]:
            suffix = generated_ids[input_length:].tolist()
            node: Dict[Any, Any] = trie
            for token_id in suffix:
                child = node.get(int(token_id))
                if not isinstance(child, dict):
                    # This should be unreachable because all prior tokens are
                    # constrained.  Returning EOS makes a bad decode fail
                    # closed instead of opening the vocabulary.
                    return [eos_token_id]
                node = child
            choices = [int(token_id) for token_id in node]
            return choices or [eos_token_id]

        kwargs: Dict[str, Any] = {
            **encoded,
            "max_new_tokens": max_choice_tokens + 1,
            "do_sample": False,
            "use_cache": True,
            "eos_token_id": eos_token_id,
            "pad_token_id": eos_token_id,
            "prefix_allowed_tokens_fn": prefix_allowed_tokens,
        }
        with self._lock, self._torch.inference_mode():
            generated = self.model.generate(**kwargs)
        generated_ids = generated[0, input_length:].tolist()
        response = self.tokenizer.decode(generated_ids, skip_special_tokens=True).strip()
        if not response.isascii() or not response.isdigit():
            raise DesktopRuntimeError("constrained decode did not produce a decimal ID")
        choice_id = int(response)
        if choice_id not in normalized_ids:
            raise DesktopRuntimeError("constrained decode selected an unknown ID")
        return choice_id


def _read_json_request(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError as exc:
        raise ValueError("invalid Content-Length") from exc
    if length < 1 or length > 2 * 1024 * 1024:
        raise ValueError("request body size is invalid")
    body = handler.rfile.read(length)
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("request body must be JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("request JSON must be an object")
    return parsed


def _messages_from_request(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise ValueError("messages must be a non-empty array")
    messages: List[Dict[str, str]] = []
    for raw in raw_messages:
        if not isinstance(raw, dict):
            raise ValueError("each message must be an object")
        role = raw.get("role")
        content = raw.get("content")
        if not isinstance(role, str) or not isinstance(content, str):
            raise ValueError("message role and content must be strings")
        if role not in {"system", "user", "assistant"}:
            raise ValueError("unsupported message role")
        messages.append({"role": role, "content": content})
    return messages


def _max_tokens_from_request(payload: Dict[str, Any]) -> int:
    value = payload.get("max_tokens", 512)
    if not isinstance(value, int) or isinstance(value, bool) or value < 16 or value > 2048:
        raise ValueError("max_tokens must be an integer from 16 to 2048")
    return value


def _allowed_ids_from_request(payload: Dict[str, Any]) -> List[int]:
    value = payload.get("allowed_ids")
    if not isinstance(value, list) or not value:
        raise ValueError("allowed_ids must be a non-empty array")
    if len(value) > 128:
        raise ValueError("allowed_ids must contain at most 128 values")
    normalized: List[int] = []
    for item in value:
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise ValueError("allowed_ids must contain non-negative integers")
        if item in normalized:
            raise ValueError("allowed_ids must not contain duplicates")
        normalized.append(item)
    return normalized


def make_handler(engine: ChatEngineProtocol, model_name: str) -> Type[BaseHTTPRequestHandler]:
    class LocalChatHandler(BaseHTTPRequestHandler):
        server_version = "RK3588ReportParserDesktop/0.1"

        def log_message(self, format: str, *args: object) -> None:
            # Prompts carry OCR text, so the server must not write request data to logs.
            return

        def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send_json(200, {"ok": True, "model": model_name, "local_only": True})
                return
            self._send_json(404, {"error": {"message": "not found"}})

        def do_POST(self) -> None:
            if self.path not in {"/v1/chat/completions", "/v1/span-choice"}:
                self._send_json(404, {"error": {"message": "not found"}})
                return
            try:
                payload = _read_json_request(self)
                messages = _messages_from_request(payload)
                if self.path == "/v1/span-choice":
                    choose = getattr(engine, "choose", None)
                    if not callable(choose):
                        raise DesktopRuntimeError("local model does not support constrained span choices")
                    allowed_ids = _allowed_ids_from_request(payload)
                    choice_id = choose(messages, allowed_ids)
                    if (
                        not isinstance(choice_id, int)
                        or isinstance(choice_id, bool)
                        or choice_id not in allowed_ids
                    ):
                        raise DesktopRuntimeError("local model returned an invalid constrained choice")
                    self._send_json(
                        200,
                        {
                            "id": "local-choice-%d" % int(time.time() * 1000),
                            "object": "span.choice",
                            "created": int(time.time()),
                            "model": model_name,
                            "choice_id": choice_id,
                        },
                    )
                    return
                content = engine.generate(messages, _max_tokens_from_request(payload))
            except (DesktopRuntimeError, ValueError) as exc:
                self._send_json(400, {"error": {"message": str(exc)}})
                return
            except Exception:
                self._send_json(500, {"error": {"message": "local model inference failed"}})
                return
            self._send_json(
                200,
                {
                    "id": "local-%d" % int(time.time() * 1000),
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model_name,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

    return LocalChatHandler


def create_server(host: str, port: int, engine: ChatEngineProtocol, model_name: str) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("desktop model server must bind to a loopback address")
    server = ThreadingHTTPServer((host, port), make_handler(engine, model_name))
    server.daemon_threads = True
    return server


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a local Qwen OpenAI-compatible test server")
    parser.add_argument("--model-path", type=Path, required=True, help="local Qwen model directory")
    parser.add_argument("--model-name", default="Qwen2.5-1.5B-Instruct")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--device", default="auto", help="auto, cpu, cuda, or cuda:N")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        engine = TransformersChatEngine(args.model_path, args.device)
        server = create_server(args.host, args.port, engine, args.model_name)
    except (DesktopRuntimeError, OSError, ValueError) as exc:
        print("ERROR: %s" % exc, flush=True)
        return 2

    address, port = server.server_address[:2]
    print("Local model server ready: http://%s:%s/health" % (address, port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
