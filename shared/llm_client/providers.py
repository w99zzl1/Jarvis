"""
Provider layer — явная инкапсуляция каждого LLM бэкенда.

Каждый провайдер несёт ответственность за:
  1. Собственную аутентификацию (SDK / base_url / заголовки).
  2. Собственный набор поддерживаемых параметров (frequency_penalty и т.п.).
  3. Отправку chat/completions запроса и приведение ответа к унифицированному формату
     (content, tool_calls, finish_reason).

Правило: строка `provider` ВСЕГДА приходит явно из UI (через фабрику),
никакого эвристического парсинга имени модели тут нет.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger("providers")

# httpx logs the full request URL at INFO, and the Gemini API key travels in
# the query string (?key=...). That writes the key into every log file, so keep
# the transport quiet unless the app explicitly turns it back on.
for _noisy in ("httpx", "httpcore", "googleapiclient"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


def _thought_signature(part: Any) -> Any:
    """Gemini 3 rejects functionCall parts sent back without this signature."""
    sig = getattr(part, "thought_signature", None)
    if sig is None:
        return None
    if isinstance(sig, (bytes, bytearray)):
        return base64.b64encode(bytes(sig)).decode("ascii")
    return sig


def _safe_json_object(raw: Any) -> dict:
    """Parse tool arguments that may be a JSON string, dict or Python repr."""
    if isinstance(raw, dict):
        return raw
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        text = raw.strip()
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError:
            pass
        # Python repr, e.g. "{'command': 'date'}"
        try:
            import ast
            parsed = ast.literal_eval(text)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except (ValueError, SyntaxError):
            return {}
    return {}


def _function_call_args(fc: Any) -> str:
    """Serialize Gemini FunctionCall arguments to a JSON string.

    google-genai hands back a plain dict in `fc.args`, so `str(...)` used to
    produce a Python repr with single quotes. That is not valid JSON, every
    `json.loads` downstream failed, and every tool call reached the agent with
    empty arguments.
    """
    args = getattr(fc, "args", None)
    if not args:
        args = getattr(fc, "partial_args", None)
    if args is None:
        return "{}"
    if isinstance(args, str):
        text = args.strip()
        return text if text.startswith("{") else "{}"
    try:
        return json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return "{}"


# ---------------------------------------------------------------------------
# Pseudo-XML recovery (shared across providers, вынесен сюда из client.py)
# ---------------------------------------------------------------------------

_PSEUDO_FUNC_RE = re.compile(
    r"<" + r"function[=\s]+(\w+)\s*[({\[]\s*(\{.*?\})\s*[)}\]]\s*(?::\s*)?>",
    re.DOTALL,
)
_TOOL_CALL_RE = re.compile(
    r"<" + r"tool_call>\s*<" + r"tool_name>\s*(\w+)\s*</" + r"tool_name>\s*<"
    + r"parameters>\s*(\{.*?\})\s*</"
    + r"parameters>\s*</"
    + r"tool_call>",
    re.DOTALL,
)


def _parse_failed_generation(text: str) -> list[dict] | None:
    if not text:
        return None
    calls: list[dict] = []

    for i, (name, args_str) in enumerate(_PSEUDO_FUNC_RE.findall(text)):
        try:
            args = json.loads(args_str)
        except json.JSONDecodeError:
            logger.warning("[RECOVER] Failed to parse args for pseudo function: %s", name)
            continue
        calls.append({
            "id": f"recovered_{i}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        })

    for i, (name, args_str) in enumerate(_TOOL_CALL_RE.findall(text)):
        try:
            args = json.loads(args_str)
        except json.JSONDecodeError:
            logger.warning("[RECOVER] Failed to parse args for tool_call: %s", name)
            continue
        calls.append({
            "id": f"recovered_tc_{i}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        })

    return calls or None


# ---------------------------------------------------------------------------
# Key manager protocol (duck-type compatible with shared.llm_client.api_key_manager)
# ---------------------------------------------------------------------------

class _KeyManager(ABC):
    @abstractmethod
    def get_key(self) -> str: ...
    @abstractmethod
    def rotate(self) -> str | None: ...


class _SingleKeyManager:
    """Lightweight fallback when user gave us a plain string key (no manager)."""
    def __init__(self, key: str):
        self._key = key
        self._rotated = False
    def get_key(self) -> str:
        return self._key
    def rotate(self) -> str | None:
        if self._rotated:
            return None
        self._rotated = True
        logger.warning("[KEYS] Single key — rotation is impossible (exhausted)")
        return None


# ---------------------------------------------------------------------------
# Unified response
# ---------------------------------------------------------------------------

@dataclass
class ChatResponse:
    content: str | None
    tool_calls: Any  # None or SDK-native list of ChatCompletionMessageToolCall
    finish_reason: str  # stop | tool_calls | fatal | rate_limited | server | error
    # Для случая, когда мы восстановили tool_calls из pseudo-XML (они в dict-формате, а не SDK)
    tool_calls_as_dicts: list[dict] | None = None
    # Для Structured Output
    structured: dict | None = None
    # Human-readable text of the provider failure (e.g. the Gemini 503 body).
    error_message: str | None = None
    # Images returned by the model (Vision responses), as base64 data URLs.
    images: list[str] | None = None


# ---------------------------------------------------------------------------
# Base provider
# ---------------------------------------------------------------------------

class BaseProvider(ABC):
    """
    Абстрактный провайдер. Наследники реализуют только:
      - _build_request_kwargs (фильтр параметров под провайдер)
      - _create_client (SDK или OpenAI-совместимый клиент с правильным base_url/key)
      - is_tool_use_failed / _extract_failed_generation (опционально для Groq)
    """

    NAME: str = "base"
    BASE_URL: str | None = None
    UNSUPPORTED_PARAMS: set[str] = set()

    def __init__(
        self,
        key_manager: _KeyManager,
        base_url_override: str | None = None,
    ):
        self._km = key_manager
        self._base_url = base_url_override or self.BASE_URL
        self._client_cache: Any = None

    # ------------------------------------------------------------------
    # Public entrypoint — унифицированный ask()
    # ------------------------------------------------------------------
    def ask(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.1,
        frequency_penalty: float = 0.3,
        extra: dict | None = None,
    ) -> ChatResponse:
        for attempt in range(3):
            try:
                client = self._get_client()
                kwargs = self._build_request_kwargs(
                    model=model,
                    messages=messages,
                    tools=tools,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    frequency_penalty=frequency_penalty,
                )
                if extra:
                    kwargs.update(extra)

                response = client.chat.completions.create(**kwargs)

                if not response.choices:
                    logger.error("[LLM] Empty choices in response")
                    return ChatResponse(None, None, "error")

                choice = response.choices[0]
                msg = choice.message
                return ChatResponse(
                    content=getattr(msg, "content", None),
                    tool_calls=getattr(msg, "tool_calls", None),
                    finish_reason=getattr(choice, "finish_reason", "stop") or "stop",
                )

            except Exception as e:
                logger.warning("[LLM] Request failed (attempt %d/3): %s", attempt + 1, e)

                # 1. Groq-specific tool_use_failed recovery (опционально)
                if self.is_tool_use_failed(e):
                    recovered = self._try_recover_pseudo_xml(e)
                    if recovered:
                        return recovered
                    logger.error("[LLM] tool_use_failed could not be recovered")
                    return ChatResponse(None, None, "fatal", error_message=str(e))

                # 2. Classify error
                code = self._classify_error(e)

                if code == "fatal":
                    logger.error("[LLM] Fatal error: %s", e)
                    return ChatResponse(None, None, "fatal", error_message=str(e))

                if code == "rate_limit":
                    logger.warning("[LLM] Rate limit, rotating API key")
                    next_key = self._km.rotate()
                    if not next_key:
                        logger.error("[LLM] No next API key available")
                        return ChatResponse(None, None, "rate_limited", error_message=str(e))
                    self._client_cache = None  # переинициируем клиент с новым ключом
                    continue

                if code == "server":
                    if attempt < 2:
                        delay = 2 * (2 ** attempt)
                        logger.warning("[LLM] Server error, retrying in %ss", delay)
                        time.sleep(delay)
                        continue
                    return ChatResponse(None, None, "server", error_message=str(e))

                logger.error("[LLM] Unhandled error: %s", e)
                return ChatResponse(None, None, "error", error_message=str(e))

        return ChatResponse(None, None, "error")

    # ------------------------------------------------------------------
    # Streaming entrypoint.
    # Yields ("delta", text_so_far) tuples and finally ("done", ChatResponse).
    # Default implementation: no real streaming, emit the whole answer at once.
    # ------------------------------------------------------------------
    def ask_stream(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.1,
        frequency_penalty: float = 0.3,
    ):
        resp = self.ask(
            model=model,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            frequency_penalty=frequency_penalty,
        )
        if resp.content:
            yield ("delta", resp.content)
        yield ("done", resp)

    # ------------------------------------------------------------------
    # Structured Output support
    # ------------------------------------------------------------------
    def ask_structured(
        self,
        model: str,
        messages: list[dict],
        schema: dict,
        max_tokens: int = 1500,
        temperature: float = 0.1,
    ) -> dict | None:
        """
        Request structured JSON output conforming to the given JSON schema.
        Only works with providers that support response_format (OpenAI, Google, NIM, custom).
        Returns parsed dict or None if not supported/failed.
        """
        # Default implementation: not supported
        logger.warning(f"[{self.NAME}] Structured output not supported by this provider")
        return None

    # ------------------------------------------------------------------
    # Абстрактные / переопределяемые хуки
    # ------------------------------------------------------------------

    @abstractmethod
    def _create_client(self, api_key: str):
        """Создать SDK/OpenAI клиент с переданным ключом и base_url этого провайдера."""

    @abstractmethod
    def _build_request_kwargs(self, **kw) -> dict:
        """Собрать kwargs для client.chat.completions.create. Тут и фильтрация параметров."""

    def is_tool_use_failed(self, err: Exception) -> bool:
        return False

    def _extract_failed_generation(self, err: Exception) -> Any:
        return None

    # ------------------------------------------------------------------
    # Общий код для всех провайдеров
    # ------------------------------------------------------------------

    def _get_client(self):
        if self._client_cache is None:
            key = self._km.get_key()
            if not key:
                raise RuntimeError(f"No API key available for provider '{self.NAME}'")
            self._client_cache = self._create_client(key)
        return self._client_cache

    def _try_recover_pseudo_xml(self, err: Exception) -> ChatResponse | None:
        failed = self._extract_failed_generation(err)
        if not failed:
            return None
        text = failed if isinstance(failed, str) else (
            failed.get("attempted_generation")
            or failed.get("generated")
            or failed.get("content")
            or failed.get("text")
            or json.dumps(failed, ensure_ascii=False)
        )
        recovered_dicts = _parse_failed_generation(text)
        if not recovered_dicts:
            logger.warning("[RECOVER] failed_generation found but no pseudo-XML parsed")
            return None
        logger.info("[RECOVER] Recovered %d tool call(s)", len(recovered_dicts))
        return ChatResponse(
            content=None,
            tool_calls=None,
            finish_reason="tool_calls",
            tool_calls_as_dicts=recovered_dicts,
        )

    def _error_status(self, err: Exception) -> int | None:
        """Status code of a provider error, whatever SDK raised it.

        The OpenAI-compatible SDKs expose `status_code`; `google.genai` raises
        `APIError` subclasses that carry the code on `.code` instead, so a
        plain `status_code` lookup returned None for every Gemini failure and
        503 UNAVAILABLE was misfiled as an unclassified "error".
        """
        for attr in ("status_code", "code", "status"):
            value = getattr(err, attr, None)
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
        response = getattr(err, "response", None)
        if response is not None:
            value = getattr(response, "status_code", None)
            if isinstance(value, int):
                return value
        return None

    def _classify_error(self, err: Exception) -> str:
        err_str = str(err).lower()
        status_code = self._error_status(err)

        # Gemini reports overload/availability problems in the message body
        # rather than only in the status code.
        transient_markers = (
            "503", "unavailable", "high demand", "overloaded",
            "timeout", "timed out", "connection", "temporarily unavailable",
            "internal error", "deadline exceeded", "429", "resource_exhausted",
            "rate limit", "too many requests",
        )
        if any(marker in err_str for marker in transient_markers):
            return "rate_limit" if ("429" in err_str or "rate limit" in err_str
                                    or "resource_exhausted" in err_str
                                    or "too many requests" in err_str) else "server"

        if status_code == 429:
            return "rate_limit"
        if status_code in {400, 401, 403, 404, 422}:
            return "fatal"
        if status_code is not None and status_code >= 500:
            return "server"

        # A malformed key surfaces as "API key not valid" without a usable code.
        if any(marker in err_str for marker in (
            "api key not valid", "invalid api key", "invalid_api_key",
            "permission denied", "unauthenticated",
        )):
            return "fatal"
        return "error"


# ===========================================================================
# 1. GroqProvider (нативный Groq SDK)
# ===========================================================================

class GroqProvider(BaseProvider):
    NAME = "groq"
    BASE_URL = None  # Groq SDK сам подставляет

    def _create_client(self, api_key: str):
        from groq import Groq
        return Groq(api_key=api_key, timeout=15.0)

    def _build_request_kwargs(self, model, messages, tools, max_tokens, temperature, frequency_penalty) -> dict:
        kwargs = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "frequency_penalty": frequency_penalty,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        return kwargs

    def is_tool_use_failed(self, err: Exception) -> bool:
        try:
            status_code = getattr(err, "status_code", None)
            if status_code != 400:
                return False
            body = getattr(err, "body", None)
            if isinstance(body, dict):
                error_data = body.get("error", body)
                if isinstance(error_data, dict) and error_data.get("code") == "tool_use_failed":
                    return True
            code = getattr(err, "code", None)
            if code == "tool_use_failed":
                return True
            return "tool_use_failed" in str(err)
        except Exception:
            return False

    def _extract_failed_generation(self, err: Exception):
        body = getattr(err, "body", None)
        if isinstance(body, dict):
            for field_path in (("error", "failed_generation"), ("failed_generation",)):
                node = body
                ok = True
                for k in field_path:
                    if not isinstance(node, dict) or k not in node:
                        ok = False
                        break
                    node = node[k]
                if ok and node is not None:
                    return node
        response = getattr(err, "response", None)
        if response is not None:
            try:
                data = response.json()
                if isinstance(data, dict):
                    for field_path in (("error", "failed_generation"), ("failed_generation",)):
                        node = data
                        ok = True
                        for k in field_path:
                            if not isinstance(node, dict) or k not in node:
                                ok = False
                                break
                            node = node[k]
                        if ok and node is not None:
                            return node
            except Exception:
                pass
        return None


# ===========================================================================
# 2. GoogleProvider (Gemini через нативный google.genai SDK)
# ===========================================================================

class GoogleProvider(BaseProvider):
    NAME = "google"
    BASE_URL = None  # Нативный SDK
    # Every other provider passes a socket timeout to its SDK client; Gemini did
    # not, so a stalled stream parked the agent thread forever and the UI sat
    # empty behind SSE keepalives. This is a read/stall timeout in ms: it resets
    # on every received chunk, so a long-but-alive stream is never cut off.
    HTTP_TIMEOUT_MS = 120_000
    UNSUPPORTED_PARAMS = {
        "frequency_penalty",
        "presence_penalty",
        "top_p",
        "logprobs",
        "n",
        "seed",
        "user",
        "response_format",
    }

    def _create_client(self, api_key: str):
        from google import genai as _genai
        from google.genai import types as _types
        return _genai.Client(
            api_key=api_key,
            http_options=_types.HttpOptions(timeout=self.HTTP_TIMEOUT_MS),
        )

    @staticmethod
    def _text_of(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    chunks.append(block.get("text", ""))
            return "\n".join(chunks)
        return "" if content is None else str(content)

    @staticmethod
    def _image_part_from_block(block: dict, _types) -> "_types.Part | None":
        """Build an inline-data Part from an image_url content block.

        Supports data: URLs (base64) produced by the frontend after upload.
        Returns None if the block cannot be converted.
        """
        if not isinstance(block, dict):
            return None
        if block.get("type") not in ("image_url", "image"):
            return None
        image_url = block.get("image_url") or block.get("url") or ""
        if not isinstance(image_url, str):
            image_url = str(image_url)
        if not image_url.startswith("data:"):
            return None
        try:
            header, b64 = image_url.split(",", 1)
            mime = header.split(";")[0].split(":")[1] if ":" in header else "application/octet-stream"
            import base64
            data = base64.b64decode(b64)
            return _types.Part.from_bytes(data=data, mime_type=mime)
        except Exception:
            return None

    def _build_contents(self, messages: list[dict], _types) -> tuple[list, list[str]]:
        """Convert OpenAI-style messages to Gemini contents + system instruction.

        The previous version only understood user/system/model, so every
        assistant turn and every tool result was silently dropped. The model
        could not see what it had already called or received, and kept
        repeating the same tool call.
        """
        system_parts: list[str] = []
        contents: list = []
        call_names: dict[str, str] = {}

        for msg in messages:
            role = msg.get("role", "user")
            text = self._text_of(msg.get("content"))

            if role == "system":
                if text.strip():
                    system_parts.append(text)
                continue

            if role in ("user", "human"):
                parts: list = []
                if isinstance(msg.get("content"), list):
                    for block in msg["content"]:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text" and block.get("text", "").strip():
                            parts.append(_types.Part(text=block["text"]))
                        elif btype in ("image_url", "image"):
                            img_part = self._image_part_from_block(block, _types)
                            if img_part is not None:
                                parts.append(img_part)
                elif text.strip():
                    parts.append(_types.Part(text=text))
                if parts:
                    contents.append(_types.Content(role="user", parts=parts))
                continue

            if role in ("assistant", "model", "ai"):
                parts = []
                if text.strip():
                    parts.append(_types.Part(text=text))
                for tc in msg.get("tool_calls") or []:
                    fn = (tc or {}).get("function") or {}
                    name = fn.get("name", "")
                    if not name:
                        continue
                    call_id = tc.get("id") or ""
                    if call_id:
                        call_names[call_id] = name
                    kwargs = {}
                    signature = fn.get("thought_signature")
                    if signature:
                        if isinstance(signature, str):
                            try:
                                signature = base64.b64decode(signature)
                            except (ValueError, TypeError):
                                signature = None
                        if isinstance(signature, (bytes, bytearray)):
                            kwargs["thought_signature"] = bytes(signature)
                    parts.append(_types.Part(
                        function_call=_types.FunctionCall(
                            name=name,
                            args=_safe_json_object(fn.get("arguments")),
                        ),
                        **kwargs,
                    ))
                if parts:
                    contents.append(_types.Content(role="model", parts=parts))
                continue

            if role == "tool":
                call_id = msg.get("tool_call_id", "")
                name = msg.get("name") or call_names.get(call_id) or "tool"
                response = _types.FunctionResponse(
                    name=name,
                    response={"output": text},
                )
                part = _types.Part(function_response=response)
                # Consecutive tool results belong to one user turn.
                if contents and getattr(contents[-1], "role", None) == "user" \
                        and all(getattr(p, "function_response", None) is not None
                                for p in (contents[-1].parts or [])):
                    contents[-1].parts.append(part)
                else:
                    contents.append(_types.Content(role="user", parts=[part]))
                continue

        return contents, system_parts

    @staticmethod
    def _normalize_tool_name(raw_name: str, allowed: set[str]) -> str | None:
        """Map Gemini's internal call names back onto a declared tool.

        Gemini sometimes emits `default_api:read_file{path:...}` — its own
        private tool syntax. Forwarding that verbatim makes the agent execute a
        tool that does not exist and then stall.
        """
        name = (raw_name or "").strip()
        if not name:
            return None
        if name in allowed:
            return name
        cleaned = name.split("{", 1)[0].strip()
        cleaned = cleaned.split(":")[-1].strip() if ":" in cleaned else cleaned
        if cleaned in allowed:
            return cleaned
        if name.startswith("default_api") or "{" in name:
            logger.warning(
                "[GoogleProvider] dropping undeclared tool call %r (declared: %s)",
                name, sorted(allowed),
            )
            return None
        return name

# ------------------------------------------------------------------
    # Retry / key rotation
    #
    # GoogleProvider predates the retry loop in BaseProvider.ask and, by
    # overriding ask()/ask_stream() without re-implementing it, a single
    # transient `503 UNAVAILABLE` from Gemini killed the whole agent run
    # after exactly one HTTP request: no backoff, no key rotation, and the
    # error was flattened to the generic token "error" so the agent could not
    # tell a capacity blip from a bad request.
    # ------------------------------------------------------------------
    ATTEMPTS = 3

    def _retryable(self, call, *args, **kwargs) -> ChatResponse:
        """Run `call` with backoff + key rotation, mirroring BaseProvider.ask."""
        last_code = "error"
        for attempt in range(self.ATTEMPTS):
            try:
                return call(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 - classified below
                code = self._classify_error(e)
                last_code = code
                logger.warning(
                    "[GoogleProvider] %s error (attempt %d/%d): %s",
                    code, attempt + 1, self.ATTEMPTS, e,
                )

                if code == "fatal":
                    return ChatResponse(None, None, "fatal", error_message=str(e))

                if code == "rate_limit":
                    if self._km.rotate():
                        self._client_cache = None
                        time.sleep(min(2 * (2 ** attempt), 20))
                        continue
                    return ChatResponse(None, None, "rate_limited", error_message=str(e))

                if code == "server":
                    if attempt < self.ATTEMPTS - 1:
                        time.sleep(min(2 * (2 ** attempt), 20))
                        continue
                    return ChatResponse(None, None, "server", error_message=str(e))

                return ChatResponse(None, None, "error", error_message=str(e))
        return ChatResponse(None, None, last_code)

    def _build_stream_request(self, messages, tools, max_tokens, temperature, _types):
        """Shared request assembly for ask()/ask_stream()."""
        contents, system_instruction = self._build_contents(messages, _types)

        gemini_tools = None
        allowed_names: set[str] = set()
        if tools:
            function_decls = []
            for tool_spec in tools:
                func = tool_spec.get("function", {})
                allowed_names.add(func.get("name", ""))
                function_decls.append(_types.FunctionDeclaration(
                    name=func.get("name", ""),
                    description=func.get("description", ""),
                    parameters=func.get("parameters", {}),
                ))
            gemini_tools = [_types.Tool(function_declarations=function_decls)]

        config_kwargs = {
            "max_output_tokens": max_tokens,
            "temperature": temperature,
        }
        if gemini_tools:
            config_kwargs["tools"] = gemini_tools
        if system_instruction:
            config_kwargs["system_instruction"] = "\n\n".join(system_instruction)

        return contents, _types.GenerateContentConfig(**config_kwargs), allowed_names

    def _build_request_kwargs(self, model, messages, tools, max_tokens, temperature, frequency_penalty) -> dict:
        return {
            "model": model,
            "max_output_tokens": max_tokens,
            "temperature": temperature,
        }

    def ask(self, model, messages, tools=None, max_tokens=1024, temperature=0.1, frequency_penalty=0.3, extra=None) -> ChatResponse:
        from google.genai import types as _types

        contents, config, allowed_names = self._build_stream_request(
            messages, tools, max_tokens, temperature, _types
        )

        def call():
            client = self._get_client()
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            return self._read_response(response, allowed_names)

        return self._retryable(call)

    def _read_response(self, response, allowed_names: set[str]) -> ChatResponse:
        """Turn a completed `generate_content` response into a ChatResponse."""
        content = None
        tool_calls: list[dict] = []
        images: list[str] = []
        finish_reason = "stop"

        try:
            if getattr(response, "text", None):
                content = response.text
            elif getattr(response, "candidates", None):
                candidate = response.candidates[0]
                if getattr(candidate, "content", None):
                    for part in candidate.content.parts:
                        if getattr(part, "text", None):
                            content = part.text
                            break
                        # Extract inline image data from Gemini response
                        inline = getattr(part, "inline_data", None)
                        if inline is not None:
                            import base64
                            mime = getattr(inline, "mime_type", "image/png")
                            data = getattr(inline, "data", b"")
                            if isinstance(data, str):
                                b64 = data
                            else:
                                b64 = base64.b64encode(data).decode("ascii")
                            images.append(f"data:{mime};base64,{b64}")
        except Exception as e:  # noqa: BLE001
            logger.warning("[GoogleProvider] Error extracting content: %s", e)

        try:
            if getattr(response, "candidates", None):
                for candidate in response.candidates:
                    if not getattr(candidate, "content", None):
                        continue
                    for idx, part in enumerate(candidate.content.parts):
                        fc = getattr(part, "function_call", None)
                        if fc is None:
                            continue
                        name = self._normalize_tool_name(
                            getattr(fc, "name", ""), allowed_names
                        )
                        if not name:
                            continue
                        finish_reason = "tool_calls"
                        tool_calls.append({
                            "id": getattr(fc, "id", "") or f"fc_{idx}",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": _function_call_args(fc),
                                "thought_signature": _thought_signature(part),
                            },
                        })
        except Exception as e:  # noqa: BLE001
            logger.warning("[GoogleProvider] Error extracting tool calls: %s", e)

        if tool_calls:
            tool_calls = [
                SimpleNamespace(
                    id=tc["id"],
                    type=tc["type"],
                    function=SimpleNamespace(
                        name=tc["function"]["name"],
                        arguments=tc["function"]["arguments"],
                        thought_signature=tc["function"].get("thought_signature"),
                    ),
                )
                for tc in tool_calls
            ]

        if not content and not tool_calls:
            logger.warning("[GoogleProvider] Empty response")

        return ChatResponse(content=content, tool_calls=tool_calls or None,
                            finish_reason=finish_reason, images=images or None)

    def ask_stream(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.1,
        frequency_penalty: float = 0.3,
    ):
        from google.genai import types as _types

        contents, config, allowed_names = self._build_stream_request(
            messages, tools, max_tokens, temperature, _types
        )

        # A mid-stream 503 discards everything received so far, so the attempt
        # is restarted from scratch. Emitting the partial text would leave the
        # model with a half-written message that it then has to reconcile.
        for attempt in range(self.ATTEMPTS):
            text_parts: list[str] = []
            tool_call_dicts: list[dict] = []
            images: list[str] = []
            finish_reason = "stop"

            try:
                stream = self._get_client().models.generate_content_stream(
                    model=model,
                    contents=contents,
                    config=config,
                )
                for chunk in stream:
                    if getattr(chunk, "candidates", None):
                        candidate = chunk.candidates[0]
                        if getattr(candidate, "content", None):
                            for part in candidate.content.parts:
                                t = getattr(part, "text", None)
                                if t:
                                    text_parts.append(t)
                                # Extract inline image data from Gemini response
                                inline = getattr(part, "inline_data", None)
                                if inline is not None:
                                    import base64
                                    mime = getattr(inline, "mime_type", "image/png")
                                    data = getattr(inline, "data", b"")
                                    if isinstance(data, str):
                                        b64 = data
                                    else:
                                        b64 = base64.b64encode(data).decode("ascii")
                                    images.append(f"data:{mime};base64,{b64}")
                                fc = getattr(part, "function_call", None)
                                if fc is None:
                                    continue
                                name = self._normalize_tool_name(
                                    getattr(fc, "name", ""), allowed_names
                                )
                                if not name:
                                    continue
                                finish_reason = "tool_calls"
                                tool_call_dicts.append({
                                    "id": getattr(fc, "id", "") or f"fc_{len(tool_call_dicts)}",
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": _function_call_args(fc),
                                        "thought_signature": _thought_signature(part),
                                    },
                                })
                    current = "".join(text_parts)
                    if current:
                        yield ("delta", current)
            except Exception as e:  # noqa: BLE001
                code = self._classify_error(e)
                logger.warning(
                    "[GoogleProvider] Stream %s error (attempt %d/%d): %s",
                    code, attempt + 1, self.ATTEMPTS, e,
                )
                if code == "fatal":
                    yield ("done", ChatResponse(None, None, "fatal", error_message=str(e)))
                    return
                if code == "rate_limit" and not self._km.rotate():
                    yield ("done", ChatResponse(None, None, "rate_limited", error_message=str(e)))
                    return
                if code in ("server", "rate_limit") and attempt < self.ATTEMPTS - 1:
                    # Deltas are cumulative snapshots, so restarting simply
                    # replaces the partial text instead of duplicating it.
                    self._client_cache = None
                    time.sleep(min(2 * (2 ** attempt), 20))
                    continue
                yield ("done", ChatResponse(None, None, code, error_message=str(e)))
                return

            break

        content = "".join(text_parts) or None

        tool_calls = [
            SimpleNamespace(
                id=tc["id"],
                type=tc["type"],
                function=SimpleNamespace(
                    name=tc["function"]["name"],
                    arguments=tc["function"]["arguments"],
                    thought_signature=tc["function"].get("thought_signature"),
                ),
            )
            for tc in tool_call_dicts
        ] if tool_call_dicts else None

        yield ("done", ChatResponse(
            content=content, tool_calls=tool_calls, finish_reason=finish_reason,
            images=images or None,
        ))

    def ask_structured(
        self,
        model: str,
        messages: list[dict],
        schema: dict,
        max_tokens: int = 1500,
        temperature: float = 0.1,
    ) -> dict | None:
        """Google Gemini supports response_format with json_schema."""
        try:
            from google.genai import types as _types
            client = self._get_client()
            contents = []
            for msg in messages:
                role = msg.get("role", "user")
                text = msg.get("content", "")
                contents.append(_types.Content(role=role, parts=[_types.Part(text=text)]))
            config = _types.GenerateContentConfig(
                max_output_tokens=max_tokens,
                temperature=temperature,
                response_schema=schema,
            )
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            if response.candidates and response.candidates[0].content:
                return json.loads(response.candidates[0].content.parts[0].text)
        except Exception as e:
            logger.warning(f"[GoogleProvider] Structured output failed: {e}")
        return None


# ===========================================================================
# 3. OpenAIProvider — нативный OpenAI
# ===========================================================================

class OpenAIProvider(BaseProvider):
    NAME = "openai"
    BASE_URL = "https://api.openai.com/v1/"
    UNSUPPORTED_PARAMS = set()

    def _create_client(self, api_key: str):
        from openai import OpenAI
        return OpenAI(api_key=api_key, base_url=self._base_url, timeout=15.0)

    def _build_request_kwargs(self, model, messages, tools, max_tokens, temperature, frequency_penalty) -> dict:
        kwargs = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "frequency_penalty": frequency_penalty,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        return kwargs

    def ask_structured(
        self,
        model: str,
        messages: list[dict],
        schema: dict,
        max_tokens: int = 1500,
        temperature: float = 0.1,
    ) -> dict | None:
        """OpenAI supports response_format with json_schema."""
        try:
            client = self._get_client()
            kwargs = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "ExecutionPlan",
                        "schema": schema,
                        "strict": True,
                    }
                },
            }
            response = client.chat.completions.create(**kwargs)
            if response.choices and response.choices[0].message.content:
                return json.loads(response.choices[0].message.content)
        except Exception as e:
            logger.warning(f"[OpenAIProvider] Structured output failed: {e}")
        return None


# ===========================================================================
# 3b. OpenRouterProvider — один OpenAI-совместимый шлюз ко многим вендорам
# ===========================================================================

class OpenRouterProvider(OpenAIProvider):
    NAME = "openrouter"
    BASE_URL = "https://openrouter.ai/api/v1/"
    # OpenRouter routes to many vendors; some reject OpenAI-only sampling params
    # with a 400, so drop the ones that commonly fail.
    UNSUPPORTED_PARAMS = {"frequency_penalty", "presence_penalty"}

    def _build_request_kwargs(self, model, messages, tools, max_tokens, temperature, frequency_penalty) -> dict:
        kwargs = super()._build_request_kwargs(
            model, messages, tools, max_tokens, temperature, frequency_penalty
        )
        for p in self.UNSUPPORTED_PARAMS:
            kwargs.pop(p, None)
        return kwargs


# ===========================================================================
# 4. OpenAICompatibleProvider — NVIDIA NIM и любые self-hosted совместимые
# ===========================================================================

class OpenAICompatibleProvider(BaseProvider):
    NAME = "openai_compat"
    UNSUPPORTED_PARAMS = {"frequency_penalty", "presence_penalty"}

    def __init__(
        self,
        key_manager: _KeyManager,
        base_url_override: str | None = None,
        provider_variant: str = "nim",
    ):
        base_default = None
        if provider_variant == "nim":
            base_default = "https://integrate.api.nvidia.com/v1/"
        effective_base = base_url_override or base_default
        if effective_base is None:
            raise ValueError(
                "OpenAICompatibleProvider requires explicit base_url_override "
                "or provider_variant with known default ('nim')."
            )
        self.BASE_URL = effective_base
        self._variant = provider_variant
        super().__init__(key_manager, effective_base)

    def _create_client(self, api_key: str):
        from openai import OpenAI
        return OpenAI(api_key=api_key, base_url=self._base_url, timeout=15.0)

    def _build_request_kwargs(self, model, messages, tools, max_tokens, temperature, frequency_penalty) -> dict:
        kwargs = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "frequency_penalty": frequency_penalty,
        }
        for p in self.UNSUPPORTED_PARAMS:
            kwargs.pop(p, None)
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        return kwargs

    def ask_structured(
        self,
        model: str,
        messages: list[dict],
        schema: dict,
        max_tokens: int = 1500,
        temperature: float = 0.1,
    ) -> dict | None:
        """OpenAI-compatible endpoints may support response_format."""
        try:
            client = self._get_client()
            kwargs = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "ExecutionPlan",
                        "schema": schema,
                        "strict": True,
                    }
                },
            }
            response = client.chat.completions.create(**kwargs)
            if response.choices and response.choices[0].message.content:
                return json.loads(response.choices[0].message.content)
        except Exception as e:
            logger.warning(f"[OpenAICompatibleProvider] Structured output failed: {e}")
        return None


# ===========================================================================
# ProviderFactory — единый способ создать провайдер по его строковому ID
# ===========================================================================

class ProviderFactory:
    """
    Создаёт экземпляр провайдера по явному имени из UI.

    Поддерживаемые имена: groq, google, openai, openrouter, nim (или nvidia, openai_compat с base_url).
    """

    KNOWN = {"groq", "google", "openai", "openrouter", "nim", "nvidia", "openai_compat"}

    @staticmethod
    def create(
        provider: str,
        key_manager=None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> BaseProvider:
        if not provider:
            raise ValueError("Provider name is required (no auto-detect anymore).")
        p = provider.strip().lower()

        if key_manager is None and not api_key:
            raise ValueError(
                f"ProviderFactory.create('{p}'): either key_manager or api_key must be provided."
            )

        if key_manager is None:
            key_manager = _SingleKeyManager(api_key)

        if p == "groq":
            return GroqProvider(key_manager=key_manager, base_url_override=base_url)

        if p == "google":
            return GoogleProvider(key_manager=key_manager, base_url_override=base_url)

        if p == "openai":
            return OpenAIProvider(key_manager=key_manager, base_url_override=base_url)

        if p == "openrouter":
            return OpenRouterProvider(key_manager=key_manager, base_url_override=base_url)

        if p in {"nim", "nvidia"}:
            return OpenAICompatibleProvider(
                key_manager=key_manager,
                base_url_override=base_url,
                provider_variant="nim",
            )

        if p in {"openai_compat", "custom"}:
            return OpenAICompatibleProvider(
                key_manager=key_manager,
                base_url_override=base_url,
                provider_variant="custom",
            )

        raise ValueError(
            f"Unknown provider '{provider}'. "
            f"Valid values: {sorted(ProviderFactory.KNOWN)}."
        )