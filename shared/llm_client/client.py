"""
LLMClient — thin wrapper over ProviderFactory.

ИЗМЕНЕНИЯ АРХИТЕКТУРЫ:
  - Никакого авто-определения провайдера по имени модели. Provider передаётся явно.
  - Вся логика по SDK / base_url / неподдерживаемым параметрам / восстановлению
    pseudo-XML / error classification инкапсулирована в providers.py.
  - LLMClient только:
      1. Создаёт нужный провайдер через ProviderFactory в __init__.
      2. В ask() делегирует вызов провайдеру.
      3. Приводит ответ к стабильному формату (обратная совместимость с agent.py:
         tool_calls должен быть итерируемым из объектов с .id / .function.name / .function.arguments)
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

from .providers import (
    BaseProvider,
    ChatResponse,
    ProviderFactory,
)

logger = logging.getLogger("llm")


class LLMClient:
    """
    Параметры:
      - provider:  ОБЯЗАТЕЛЬНЫЙ. Один из {"groq", "google", "openai", "openrouter", "nim", "openai_compat"}.
      - model:     ОБЯЗАТЕЛЬНЫЙ. Строка модели для этого провайдера.
      - key_manager или api_key: один из двух.
      - base_url:  опциональный переопределение эндпоинта (например, для кастомного self-hosted).
    """

    def __init__(
        self,
        key_manager: Any | None = None,
        api_key: str | None = None,
        model: str = "",
        base_url: str | None = None,
        provider: str | None = None,
    ):
        if not provider:
            raise ValueError(
                "LLMClient: 'provider' argument is required since auto-detect was removed. "
                "Pass one of: groq, google, openai, openrouter, nim, openai_compat."
            )
        if not model:
            raise ValueError("LLMClient: 'model' argument is required.")

        # The agent only ever calls the chat endpoint. An image/tts/embedding
        # model would produce a bare 400 and then be retried,
        # so reject it here where the message can still be useful.
        from .model_fetcher import _NON_CHAT_PATTERNS
        if _NON_CHAT_PATTERNS.search(model):
            raise ValueError(
                f"LLMClient: '{model}' is not a chat model (image, speech, "
                f"embedding or moderation model). Pick a chat model instead."
            )

        self.model: str = model
        self.provider: str = provider.strip().lower()

        self._provider_impl: BaseProvider = ProviderFactory.create(
            provider=self.provider,
            key_manager=key_manager,
            api_key=api_key,
            base_url=base_url,
        )

    # ----------------------------------------------------------------------
    # Public ask API — стабильная сигнатура для agent.py
    # ----------------------------------------------------------------------

    def ask(
        self,
        messages,
        tools=None,
        max_tokens=1024,
        temperature=0.1,
        frequency_penalty=0.3,
        cancel=None,
    ):
        """
        Возвращает (content, tool_calls, finish_reason).
        Формат как и раньше, чтобы не переписывать весь agent.py.
        """
        # Try primary provider first
        resp: ChatResponse = self._provider_impl.ask(
            model=self.model,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            frequency_penalty=frequency_penalty,
            cancel=cancel,
        )

        # Record request for rate limit tracking
        try:
            content = resp.content or ""
            tool_calls_str = str(resp.tool_calls) if resp.tool_calls else ""
            estimated_tokens = (len(content) + len(tool_calls_str)) // 4
            if hasattr(self._provider_impl, 'record_request'):
                self._provider_impl.record_request(self.provider, estimated_tokens)
        except Exception:
            pass

        # A rate-limited response is returned to the agent's own rate limiter,
        # which owns the conversation and can shrink/retry it. Cross-provider
        # fallback was never wired (no fallback keys/models reach this client)
        # and would also need a per-provider model id, so it was removed rather
        # than left as dead API.
        # Случай 1: нативные tool_calls из SDK — возвращаем как есть, agent.py умеет с ними работать
        native = resp.tool_calls
        if native is not None:
            return resp.content, native, resp.finish_reason

        # Случай 2: Groq recovery дал dict-список. Надо привести к SDK-like объектам с атрибутами.
        as_dicts = resp.tool_calls_as_dicts
        if as_dicts:
            wrapped = [
                SimpleNamespace(
                    id=d["id"],
                    function=SimpleNamespace(
                        name=d["function"]["name"],
                        arguments=d["function"]["arguments"],
                    ),
                    type="function",
                )
                for d in as_dicts
            ]
            return resp.content, wrapped, resp.finish_reason

        # Случай 3: нет tool_calls вовсе — обычный текстовый ответ или ошибка
        return resp.content, None, resp.finish_reason

    def ask_stream(
        self,
        messages,
        tools=None,
        max_tokens=1024,
        temperature=0.1,
        frequency_penalty=0.3,
        cancel=None,
    ):
        """
        Стриминговая версия ask().
        Yields ("delta", text_so_far) и в конце
        ("done", (content, tool_calls, finish_reason, error_message)).
        Формат tool_calls такой же, как у ask(): SDK-объекты с атрибутами.
        """
        gen = self._provider_impl.ask_stream(
            model=self.model,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            frequency_penalty=frequency_penalty,
            cancel=cancel,
        )
        for kind, payload in gen:
            if kind == "delta":
                yield kind, payload
                continue
            # kind == "done": payload is ChatResponse
            resp = payload
            native = resp.tool_calls
            as_dicts = resp.tool_calls_as_dicts
            final = None
            if as_dicts and native is None:
                final = (
                    resp.content,
                    [
                        SimpleNamespace(
                            id=d["id"],
                            function=SimpleNamespace(
                                name=d["function"]["name"],
                                arguments=d["function"]["arguments"],
                            ),
                            type="function",
                        )
                        for d in as_dicts
                    ],
                    resp.finish_reason,
                    resp.error_message,
                )
            else:
                final = (resp.content, native, resp.finish_reason, resp.error_message)
            yield "done", final

    # ----------------------------------------------------------------------
    # Structured Output API
    # ----------------------------------------------------------------------

    def ask_structured(
        self,
        messages: list[dict],
        schema: dict,
        max_tokens: int = 1500,
        temperature: float = 0.1,
    ) -> dict | None:
        """
        Request structured JSON output conforming to the given JSON schema.
        Returns parsed dict or None if not supported/failed.
        """
        return self._provider_impl.ask_structured(
            model=self.model,
            messages=messages,
            schema=schema,
            max_tokens=max_tokens,
            temperature=temperature,
        )
