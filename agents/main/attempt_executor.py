"""Single attempt execution logic for the agent."""

import logging
import time
from typing import Any

from shared.llm_client import LLMClient
from shared.cancellation import AgentCancelled, CancelToken, sleep_interruptible
from .rate_limiter import RateLimiter, parse_rate_limit
from .tool_executor import ToolExecutor
from .loop_detector import LoopDetector
from .history import EventLog, ContextBuilder
from .tools.registry import ToolRegistry, ToolResult
from .agent import (
    AgentState,
    AgentConfig,
    MAX_OUTPUT_BUDGET,
    LOOP_NUDGE_NOTE,
)

logger = logging.getLogger("agent.attempt_executor")


MAX_EMPTY_RETRIES = 2
MAX_TRUNCATION_RETRIES = 1
# Provider token-window rejections are retried by shrinking the prompt. Bounded
# so a prompt that cannot fit at all still fails instead of looping.
MAX_CONTEXT_TRIM_RETRIES = 3
TOOL_OUTPUT_LIMIT = 6000


class AttemptExecutor:
    """Executes a single agent attempt."""

    def __init__(
        self,
        registry: ToolRegistry,
        system_prompt: str,
        config: AgentConfig,
        event_sink=None,
        llm_provider: str | None = None,
        event_log: EventLog | None = None,
        context: ContextBuilder | None = None,
        cancel_token: CancelToken | None = None,
        loop_nudges: int = 0,
        iterations_used: int = 0,
        tool_calls_used: int = 0,
        plan_store=None,
        plan_key: str | None = None,
    ):
        self.registry = registry
        self.system_prompt = system_prompt
        self.config = config
        self._event_sink = event_sink
        self.llm_provider = llm_provider
        # The turn's cancellation flag. Checked between iterations, on every
        # streamed delta, and handed to the shell tool so a running command is
        # killed the moment the user presses Stop.
        self.cancel_token = cancel_token

        # Sharing the agent's log/context is what makes multi-turn memory work:
        # the model sees earlier turns, tool results stay in the transcript, and
        # the server can persist `agent.log.events` when the turn ends.
        self.log = event_log if event_log is not None else EventLog()
        self.context = context if context is not None else ContextBuilder(
            self.log, system_prompt, max_tokens=config.max_context_tokens,
        )
        self.rate_limiter = RateLimiter(config)
        # The executor writes every tool result into the same EventLog that is
        # replayed to the model on the next iteration.
        self.tool_executor = ToolExecutor(
            registry, event_sink, llm_provider, event_log=self.log,
        )
        self.loop_detector = LoopDetector()
        # Shared with the Agent across retries: a warning ignored in attempt 1
        # counts the same as one ignored in attempt 2.
        self.loop_nudges = loop_nudges
        # Budgets are per user turn, not per retry. A retry continues the same
        # conversation, so it must not receive a fresh set of tool calls.
        self.iterations_used = iterations_used
        self.tool_calls_used = tool_calls_used
        self.plan_store = plan_store
        self.plan_key = plan_key
        self._last_plan_sig: str | None = None

    def _emit(self, event: dict):
        if self._event_sink:
            try:
                self._event_sink(event)
            except Exception:
                pass

    def _refresh_plan_reminder(self) -> None:
        """Re-inject the live task plan as an internal system reminder.

        Claude Code keeps todos visible in the conversation instead of hoping
        the model remembers a single tool result from twenty calls ago. Only
        the code agent has `update_plan`; the reminder is skipped elsewhere.
        """
        if self.plan_store is None:
            return
        try:
            self.registry.get("update_plan")
        except KeyError:
            return
        text = self.plan_store.describe_current(key=self.plan_key)
        if not text:
            return
        signature = text
        if signature == self._last_plan_sig:
            return
        self._last_plan_sig = signature
        self.log.add({"role": "user", "content": text, "internal": True})

    def build_tools_for_llm(self, loaded_tool_names: set[str]) -> list[dict]:
        """Build the tool list exposed to the LLM."""
        specs = self.registry.get_llm_specs(
            group_filter="always",
            include_names=loaded_tool_names,
        )
        loaded = list(specs)
        if any(s.get("function", {}).get("name") == "get_tools" for s in loaded):
            loaded.sort(
                key=lambda spec: 0 if spec.get("function", {}).get("name") == "get_tools" else 1
            )
        return loaded

    def execute_attempt(
        self,
        llm: LLMClient,
        model_name: str,
        user_input: str,
        loaded_tool_names: set[str],
        attempt_index: int,
        aggressive_trim: bool = False,
        user_internal: bool = False,
    ) -> dict:
        """Execute a single attempt and return result dict."""
        out = {
            "state": AgentState.THINKING.value,
            "output": None,
            "tool_calls_total": 0,
            "iterations": 0,
            "error": None,
            "error_message": None,
            "tool_log": [],
            "duration": 0.0,
            "model": model_name,
            "attempt_index": attempt_index,
        }

        tool_result_cache: dict[str, ToolResult] = {}
        tool_log: list[dict] = []
        # Carry the previous attempt's usage into this one.
        tool_calls_total = self.tool_calls_used
        iter_count = self.iterations_used
        empty_response_streak = 0
        # See agent._attempt_once: a truncated response is retried with more
        # headroom, then failed loudly — never presented as a finished answer.
        output_budget = self.config.max_output_tokens
        truncation_streak = 0
        # See agent._attempt_once: a prompt the provider rejects as too large is
        # rebuilt under a smaller budget and retried.
        context_budget: int | None = None
        context_trim_retries = 0

        start = time.time()
        try:
            # Add user input for first iteration. Rate-limit retries pass "" and
            # must not create a phantom empty user turn in the transcript.
            if user_input:
                if user_internal:
                    self.log.add({
                        "role": "user",
                        "content": user_input,
                        "internal": True,
                    })
                else:
                    self.log.add_user(user_input)

            for iteration in range(self.iterations_used + 1, self.config.max_iterations + 1):
                iter_count = iteration
                # Stop before spending another provider call on a turn the user
                # already cancelled.
                if self.cancel_token is not None:
                    self.cancel_token.check()

                self._refresh_plan_reminder()

                # `max_execution_time` used to be declared but never checked.
                # This is a runaway-turn guard, not a work budget: 600s for the
                # main agent, 1200s for the code agent.
                if self.config.max_execution_time and time.time() - start > self.config.max_execution_time:
                    logger.warning(
                        "[ATTEMPT] time budget exhausted after %ds",
                        self.config.max_execution_time,
                    )
                    out.update({
                        "state": AgentState.BUDGET_EXCEEDED.value,
                        "iterations": iteration,
                        "error": f"max_execution_time({self.config.max_execution_time})_exceeded",
                        "tool_calls_total": tool_calls_total,
                        "tool_log": tool_log,
                        "duration": time.time() - start,
                    })
                    return out

                max_tokens = min(self.context.calc_max_tokens(), output_budget)
                messages = self.context.build(
                    provider=self.llm_provider,
                    aggressive=aggressive_trim,
                    token_budget=context_budget,
                )

                content = None
                tool_calls = None
                _finish = "stop"
                provider_error_message = None
                streamed_text = ""
                had_stream = False
                last_stream_emit = 0.0

                stream_kwargs: dict = {
                    "tools": self.build_tools_for_llm(loaded_tool_names),
                    "max_tokens": max_tokens,
                }
                if self.cancel_token is not None:
                    stream_kwargs["cancel"] = self.cancel_token
                for kind, payload in llm.ask_stream(messages, **stream_kwargs):
                    # Checked every chunk: a long generation stops within one
                    # token of the user pressing Stop.
                    if self.cancel_token is not None:
                        self.cancel_token.check()
                    if kind == "delta":
                        streamed_text = payload
                        now = time.time()
                        if now - last_stream_emit >= 0.08:
                            last_stream_emit = now
                            had_stream = True
                            self._emit({
                                "type": "stream",
                                "text": streamed_text,
                                "iteration": iteration,
                            })
                    elif kind == "done":
                        provider_error_message = (
                            payload[3] if len(payload) > 3 else None
                        )
                        content, tool_calls, _finish = payload[:3]
                        break

                if had_stream and streamed_text:
                    self._emit({
                        "type": "stream",
                        "text": streamed_text,
                        "iteration": iteration,
                    })
                if (
                    content is None
                    and streamed_text
                    and _finish in ("stop", "length", "tool_calls")
                ):
                    content = streamed_text

                self._emit({
                    "type": "llm_responded",
                    "attempt": attempt_index,
                    "iteration": iteration,
                    "had_content": bool(content and content.strip()),
                    "tool_calls_count": len(tool_calls) if tool_calls else 0,
                    "thought_preview": (content[:200] if content else ""),
                })

                # A truncated response is not a finished answer: retry once with
                # more headroom, then fail loudly. Checked before the empty
                # branch so a MAX_TOKENS cut that spent its budget on reasoning
                # is not misread as an empty reply. Mirrors agent._attempt_once.
                if _finish == "length":
                    if truncation_streak < MAX_TRUNCATION_RETRIES:
                        truncation_streak += 1
                        output_budget = min(output_budget * 2, MAX_OUTPUT_BUDGET)
                        logger.warning(
                            "[ATTEMPT] response truncated (max_output_tokens), "
                            "retry %d/%d with budget=%d",
                            truncation_streak, MAX_TRUNCATION_RETRIES, output_budget,
                        )
                        continue
                    detail = (
                        f"Ответ обрезан: достигнут лимит вывода "
                        f"({output_budget} токенов). Разбей задачу на части."
                    )
                    self._emit({"type": "execution_error", "error": detail})
                    out.update({
                        "state": AgentState.FAILED.value,
                        "iterations": iteration,
                        "error": "response_truncated(max_output_tokens)",
                        "output": None,
                        "tool_calls_total": tool_calls_total,
                        "tool_log": tool_log,
                    })
                    return out
                truncation_streak = 0

                if content is None and tool_calls is None:
                    if _finish == "context_too_large":
                        if context_trim_retries < MAX_CONTEXT_TRIM_RETRIES:
                            context_trim_retries += 1
                            used = self.context.estimate_tokens(messages)
                            context_budget = max(1500, int(used * 0.6))
                            logger.warning(
                                "[ATTEMPT] prompt rejected as too large; retry "
                                "%d/%d with a ~%d-token budget",
                                context_trim_retries, MAX_CONTEXT_TRIM_RETRIES,
                                context_budget,
                            )
                            continue
                        self._emit({
                            "type": "execution_error",
                            "error": (
                                "Контекст не помещается в лимит провайдера даже "
                                "после сжатия. Начните новый чат или выберите "
                                "модель с большим лимитом."
                            ),
                        })
                        out.update({
                            "state": AgentState.FAILED.value,
                            "iterations": iteration,
                            "error": "context_too_large",
                            "error_message": provider_error_message,
                            "output": None,
                            "tool_calls_total": tool_calls_total,
                            "tool_log": tool_log,
                        })
                        return out
                    if _finish in ("rate_limited", "fatal", "server", "error"):
                        detail = (provider_error_message or "").strip()
                        self._emit({
                            "type": "execution_error",
                            "error": (
                                f"Provider error ({_finish}): {detail}"
                                if detail else f"Provider error ({_finish})"
                            ),
                        })
                        out.update({
                            "state": AgentState.FAILED.value,
                            "iterations": iteration,
                            "error": _finish,
                            "error_message": provider_error_message,
                            "output": None,
                            # A provider error ends this iteration, not the
                            # turn. Keep the work already done so a retry
                            # continues instead of resetting the budget.
                            "tool_calls_total": tool_calls_total,
                            "tool_log": tool_log,
                        })
                        return out
                    empty_response_streak += 1
                    logger.warning(f"[ATTEMPT] empty response, streak={empty_response_streak}")
                    if empty_response_streak > MAX_EMPTY_RETRIES:
                        out.update({
                            "state": AgentState.FAILED.value,
                            "iterations": iteration,
                            "error": "LLM returned no response after retries",
                            "tool_calls_total": tool_calls_total,
                            "tool_log": tool_log,
                        })
                        return out
                    # Internal retry prompt: never persisted, never shown in the
                    # history drawer, and written in the user's own language so
                    # the model does not switch to English mid-conversation.
                    self.log.add({
                        "role": "user",
                        "content": (
                            "[Системное сообщение: ты не ответил пользователю. "
                            "Ответь сейчас же на языке пользователя — кратко и по делу.]"
                        ),
                        "internal": True,
                    })
                    continue

                empty_response_streak = 0

                tc_list: list[dict] | None = None
                if tool_calls:
                    tc_list = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                                "thought_signature": getattr(
                                    tc.function, "thought_signature", None
                                ),
                            },
                        }
                        for tc in tool_calls
                    ]

                if tool_calls and self.loop_detector.detect_loop(tc_list):
                    self.log.add_assistant(content, tc_list)
                    for tc in tool_calls:
                        self.log.add_tool_result(
                            tc.id, tc.function.name,
                            "[not executed: loop pattern detected]",
                            result_status="error", duration=0.0,
                        )
                    # First offense is a course correction, not a failure.
                    # The note is internal (hidden in the UI) but stays in the
                    # transcript so the model can actually change tactics.
                    limit = max(0, int(self.config.loop_nudges_before_abort))
                    if self.loop_nudges < limit:
                        self.loop_nudges += 1
                        logger.warning(
                            "[ATTEMPT] loop pattern, nudge %d/%d — changing tactics",
                            self.loop_nudges, limit,
                        )
                        # Claude Code surfaces repetition as an internal
                        # reminder to the model, not as a chat message. Nothing
                        # is emitted to the user; only backend logs record it.
                        self.log.add({
                            "role": "user",
                            "content": LOOP_NUDGE_NOTE,
                            "internal": True,
                        })
                        continue

                    out.update({
                        "state": AgentState.LOOP_EXCEEDED.value,
                        "iterations": iteration,
                        "error": (
                            f"Loop pattern detected after {self.loop_nudges} "
                            "ignored warning(s)"
                        ),
                        "tool_calls_total": tool_calls_total,
                        "tool_log": tool_log,
                        "duration": time.time() - start,
                    })
                    return out

                self.log.add_assistant(content, tc_list)

                if not tool_calls:
                    # Claude Code-style: text without a tool call IS the end of
                    # the turn. No "act now" note is injected. The system prompt
                    # tells the model to act in the same reply; if it still only
                    # announces an action, that is a model error we surface in
                    # the answer, not something the loop silently papers over.
                    out.update({
                        "state": AgentState.FINISHED.value,
                        "iterations": iteration,
                        "output": content,
                        "tool_calls_total": tool_calls_total,
                        "tool_log": tool_log,
                        "duration": time.time() - start,
                    })
                    return out

                # Execute tools. `execute_all` already appends every executed
                # signature to the history list it is given; appending them
                # again here made one iteration's calls look like an ABAB loop
                # (e.g. two different shell commands B, C became B,C,B,C) and
                # killed otherwise healthy turns with "loop_exceeded".
                executed = self.tool_executor.execute_all(
                    tool_calls, tc_list, self.loop_detector.history,
                    attempt_index=attempt_index,
                    sig_cache=tool_result_cache,
                    loaded_tool_names=loaded_tool_names,
                    cancel=self.cancel_token,
                )
                tool_calls_total += len(tool_calls)
                tool_log.extend(executed)
                out.update({"output": content or f"Executed {len(tool_calls)} tool(s)"})

                if tool_calls_total >= self.config.max_tool_calls:
                    out.update({
                        "state": AgentState.BUDGET_EXCEEDED.value,
                        "iterations": iteration,
                        "error": f"tool_call_budget_exhausted({tool_calls_total})",
                        "tool_calls_total": tool_calls_total,
                        "tool_log": tool_log,
                        "duration": time.time() - start,
                    })
                    return out

            out.update({
                "state": AgentState.LOOP_EXCEEDED.value,
                "iterations": iter_count,
                "error": f"max_iterations({self.config.max_iterations})_exceeded",
                "tool_calls_total": tool_calls_total,
                "tool_log": tool_log,
                "duration": time.time() - start,
            })
            return out

        except AgentCancelled:
            out.update({
                "state": AgentState.CANCELLED.value,
                "iterations": iter_count,
                "error": "cancelled",
                "output": None,
                "tool_calls_total": tool_calls_total,
                "tool_log": tool_log,
                "duration": time.time() - start,
            })
            return out
        except Exception as e:
            out.update({
                "state": AgentState.FAILED.value,
                "iterations": iter_count,
                "error": f"Exception: {e}",
                "tool_calls_total": tool_calls_total,
                "tool_log": tool_log,
                "duration": time.time() - start,
            })
            return out

    def handle_rate_limit(
        self,
        attempt: dict,
        llm: LLMClient,
        model_name: str,
        loaded_tool_names: set[str],
        attempt_index: int,
    ) -> dict:
        """Handle rate limit by waiting and retrying the same attempt."""
        rl_retries = 0
        rl_waited = 0.0
        max_rl_retries = max(1, self.config.rate_limit_retries)
        max_rl_wait = max(30.0, self.config.rate_limit_wait)

        while attempt.get("error") == "rate_limited":
            self.rate_limiter.record_hit()
            info = parse_rate_limit(attempt.get("error_message"))
            should_retry, wait = self.rate_limiter.should_retry(info, rl_retries, rl_waited)
            if not should_retry:
                break

            rl_retries += 1
            rl_waited += wait
            logger.info(
                "[ATTEMPT] Rate limited (RPM); waiting %.1fs then retrying "
                "(retry %d/%d).", wait, rl_retries, max_rl_retries,
            )
            self._emit({
                "type": "agent_message",
                "message": f"Rate limit (RPM) — waiting {wait:.0f}s and continuing…",
            })
            try:
                sleep_interruptible(wait, self.cancel_token)
            except AgentCancelled:
                # A multi-minute rate-limit wait must not outlive the user's
                # Stop press. Report the turn as cancelled rather than retrying.
                attempt = dict(attempt)
                attempt.update({
                    "state": AgentState.CANCELLED.value,
                    "error": "cancelled",
                    "output": None,
                })
                return attempt
            # A rate-limit retry continues the same turn. Carry the counters
            # forward so the retry cannot start with a fresh budget.
            self.iterations_used = attempt.get("iterations", self.iterations_used)
            self.tool_calls_used = attempt.get("tool_calls_total", self.tool_calls_used)
            attempt = self.execute_attempt(
                llm, model_name, "", loaded_tool_names, attempt_index,
                aggressive_trim=self.rate_limiter.aggressive_trim,
            )

        return attempt