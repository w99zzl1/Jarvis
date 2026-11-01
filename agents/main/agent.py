import inspect
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

from .history import EventLog, ContextBuilder
from shared.llm_client import LLMClient
from .verifier import Verifier, AttemptDecision
from .tools.registry import (
    ToolRegistry, ToolResult, VerificationStatus, AttemptStatus,
)

logger = logging.getLogger("agent")

MAX_ITERATIONS_DEFAULT = 15
MAX_TOOL_CALLS_DEFAULT = 15
MAX_EXECUTION_TIME_DEFAULT = 60
# Tool output used to be cut at 150 chars, which for a directory listing left
# the model unable to read the entries it had just asked for — and with the
# duplicate call replayed as a stub it had no way to recover.
TOOL_OUTPUT_LIMIT = 6000

# Ceiling on how long one attempt may spend idling on rate limits before giving
# up. Per attempt; the code agent raises it in its own AgentConfig because one
# task there needs far more provider calls than a chat reply.
MAX_RATE_LIMIT_RETRIES = 3
MAX_RATE_LIMIT_WAIT = 90.0


class AgentState(Enum):
    IDLE = "idle"
    THINKING = "thinking"
    EXECUTING = "executing"
    FINISHED = "finished"
    FAILED = "failed"
    LOOP_EXCEEDED = "loop_exceeded"
    BUDGET_EXCEEDED = "budget_exceeded"
    VERIFIED_FAILED = "verified_failed"


class AgentLoopDetected(Exception):
    pass


class AgentBudgetExceeded(Exception):
    pass


@dataclass
class AgentConfig:
    max_iterations: int = MAX_ITERATIONS_DEFAULT
    max_tool_calls: int = MAX_TOOL_CALLS_DEFAULT
    max_execution_time: int = MAX_EXECUTION_TIME_DEFAULT
    primary_attempts: int = 2
    max_context_tokens: int = 28000
    # Output budget per LLM call. 600 suits voice-sized answers, but a code
    # agent must emit whole files inside one tool call: at 600 the provider
    # truncates the arguments mid-string and Groq rejects them as invalid JSON.
    max_output_tokens: int = 600
    # How long one attempt may spend idling on a per-minute throttle. The main
    # agent answers in a few calls, so a short leash keeps chat responsive; a
    # code agent may need dozens of calls, and free tiers only allow 5-30 RPM.
    rate_limit_retries: int = MAX_RATE_LIMIT_RETRIES
    rate_limit_wait: float = MAX_RATE_LIMIT_WAIT


@dataclass
class AttemptRecord:
    """Public record of one attempt, serialized to trace."""
    success: bool
    attempt_status: str
    aggregate: str
    reasoning: str
    output: str | None
    state: str
    iterations: int
    tool_calls_total: int
    tool_log: list[dict]
    error: str | None
    verification_status: str | None
    verification_reason: str | None
    model: str
    attempt_index: int
    duration: float
    error_message: str | None = None


@dataclass
class AgentResult:
    success: bool
    output: str | None = None
    state: str = "finished"
    iterations: int = 0
    tool_calls_total: int = 0
    trace_id: str | None = None
    error: str | None = None
    model_used: str = "unknown"
    attempts: int = 0
    verification_status: str | None = None
    verification_reason: str | None = None
    attempt_status: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class ExecutionTrace:
    def __init__(self, trace_id: str, user_input: str, model_used: str = "llama-3.1-8b-instant"):
        self.trace_id = trace_id
        self.user_input = user_input
        self.started_at = time.time()
        self.attempts: list[dict] = []
        self.model_used = model_used
        self.verification_status: str | None = None
        self.verification_reason: str | None = None
        self.final_output: str | None = None
        self.final_state: str = "pending"
        self.total_tool_calls = 0

    def record(self, ar: AttemptRecord):
        self.attempts.append(asdict(ar))
        self.total_tool_calls += ar.tool_calls_total

    def finalize(self, output: str | None, state: AgentState, total_time: float):
        self.final_output = output
        self.final_state = state.value
        self.total_time = round(total_time, 3)

    def save(self):
        from shared import paths

        trace_dir = str(paths.traces_dir())
        path = os.path.join(trace_dir, f"{self.trace_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "trace_id": self.trace_id,
                "user_input": self.user_input,
                "started_at": self.started_at,
                "total_time": getattr(self, "total_time", 0),
                "final_state": self.final_state,
                "final_output": self.final_output,
                "model_used": self.model_used,
                "verification_status": self.verification_status,
                "verification_reason": self.verification_reason,
                "total_tool_calls": self.total_tool_calls,
                "attempts": self.attempts,
            }, f, ensure_ascii=False, indent=2)
        logger.info(f"[TRACE] Saved to {path}")


def build_retry_reason(prev_ar: AttemptRecord) -> str:
    lines = [
        "Previous attempt was rejected by the verifier.",
        "",
        f"Model             : {prev_ar.model}",
        f"Agent state       : {prev_ar.state}",
        f"Aggregate verify  : {prev_ar.aggregate}",
        f"Verifier reason   : {prev_ar.verification_reason}",
        f"Tool calls        : {prev_ar.tool_calls_total}",
    ]
    if prev_ar.error:
        lines.append(f"Error             : {prev_ar.error}")
    if prev_ar.output:
        first_lines = "\n".join(prev_ar.output.strip().splitlines()[:6])
        lines.append("")
        lines.append("Last output preview:")
        lines.append(first_lines)
    lines.append("")
    lines.append("Do not repeat the same strategy. If disk/process verification is "
                 "needed, follow up with a separate visible tool call.")
    return "\n".join(lines)


def _get_retry_delay(error: str) -> float:
    """Return the number of seconds to wait before a retry attempt.
    
    Dynamic delays based on error type:
    - rate_limited: longer wait (5s) to respect API RPM limits
    - tool_execution errors: medium wait (2s)
    - other errors: short wait (1s)
    """
    error_lower = (error or "").lower()
    if "rate_limited" in error_lower:
        return 5.0
    # Provider capacity problems (Gemini 503 UNAVAILABLE, overloaded) need a
    # real pause — retrying after 1s just hits the same limit.
    if "server" in error_lower or "503" in error_lower or "unavailable" in error_lower:
        return 8.0
    if "timeout" in error_lower or "connection" in error_lower:
        return 3.0
    # Default short delay for other errors
    return 1.0


# A Google 429 is normally a *per-minute* throttle: the body carries a
# `retryDelay` telling us exactly how long to wait, so the turn can continue.
# A per-day quota is different - waiting is pointless. These patterns tell the
# two apart and pull the delay out of the raw provider error string.
_RETRY_DELAY_RE = re.compile(
    r"retrydelay[\"']?\s*[:\s]\s*[\"']?([0-9]+(?:\.[0-9]+)?)s", re.IGNORECASE
)
_RETRY_IN_RE = re.compile(r"retry in ([0-9]+(?:\.[0-9]+)?)s", re.IGNORECASE)
_PER_DAY_RE = re.compile(r"per\s*day", re.IGNORECASE)



@dataclass
class RateLimitInfo:
    delay: float
    per_day: bool


def _parse_rate_limit(message: str | None) -> RateLimitInfo | None:
    """Decode a provider 429 body: how long to wait, and whether it is per-day."""
    if not message:
        return None
    per_day = bool(_PER_DAY_RE.search(message))
    match = _RETRY_DELAY_RE.search(message) or _RETRY_IN_RE.search(message)
    delay = float(match.group(1)) if match else 5.0
    return RateLimitInfo(delay=delay, per_day=per_day)


class Agent:
    def __init__(
        self,
        registry: ToolRegistry,
        primary_llm: LLMClient,
        system_prompt: str = "",
        config: AgentConfig | None = None,
        event_sink=None,
    ):
        self.registry = registry
        self.primary_llm = primary_llm
        self.system_prompt = self._build_prompt_with_catalog(system_prompt)
        self.config = config or AgentConfig()
        self.log = EventLog()
        self._loaded_tool_names: set[str] = set()
        self.context = self._make_context(self.system_prompt)
        self.state = AgentState.IDLE
        self._trace: ExecutionTrace | None = None
        self.verifier = Verifier()
        self._event_sink = event_sink
        # Rate-limit pressure tracker. Trimming stays off until the
        # provider has rejected us a couple of times in this session, so
        # normal (non-limited) chats keep full context.
        self._rate_limit_hits = 0
        self._aggressive_trim = False

    def _make_context(self, system_prompt: str) -> ContextBuilder:
        return ContextBuilder(
            self.log, system_prompt, max_tokens=self.config.max_context_tokens,
        )

    def _build_prompt_with_catalog(self, base_prompt: str) -> str:
        """Catalog is sent via tools= parameter; keep system prompt lean."""
        return base_prompt

    def _build_tools_for_llm(self) -> list[dict]:
        """Build the tool list exposed to the LLM.

        'always' group tools go up front. On-demand tools are normally hidden,
        but once get_tools has loaded one its schema stays exposed for the rest
        of the turn, otherwise the model could never actually call it.
        """
        specs = self.registry.get_llm_specs(
            group_filter="always",
            include_names=self._loaded_tool_names,
        )
        loaded = list(specs)
        if any(s.get("function", {}).get("name") == "get_tools" for s in loaded):
            loaded.sort(
                key=lambda spec: 0 if spec.get("function", {}).get("name") == "get_tools" else 1
            )
        return loaded

    def set_event_sink(self, sink):
        """Re-point streaming events at the current request's sink.

        A session-reused agent outlives the request that created it, so the bus
        captured in __init__ belongs to a previous turn. Every request calls this
        before `run` so events reach the right SSE stream.
        """
        self._event_sink = sink

    def load_history(self, events: list[dict]):
        """Seed this agent with a previously persisted conversation.

        Lets a freshly built agent (switched model, or a new process after a
        restart) continue the ongoing conversation instead of starting empty.
        """
        self.log.clear()
        for event in events or []:
            self.log.add(event)

    def _emit(self, event: dict):
        if self._event_sink:
            try:
                self._event_sink(event)
            except Exception:
                pass

    def run(self, user_input: str) -> AgentResult:
        trace_id = str(uuid.uuid4())[:8]
        primary_model = getattr(self.primary_llm, "model", "unknown")
        self._trace = ExecutionTrace(trace_id, user_input, model_used=primary_model)
        self.state = AgentState.THINKING
        self._loaded_tool_names.clear()

        start_time = time.time()
        attempts_used = 0
        last_record: AttemptRecord | None = None

        self.registry.reset_all()

        for attempt_index in range(1, self.config.primary_attempts + 1):
            attempts_used = attempt_index

            if attempt_index == 1:
                self.log.add_user(user_input)
            elif last_record is not None:
                reason = build_retry_reason(last_record)
                # Internal retry instructions - needed for LLM context but
                # must never reach the UI as a visible user/system message.
                self.log.add({
                    "role": "user",
                    "content": (
                        f"[System message: this is retry attempt {attempt_index} for the primary model. "
                        f"Previous attempt was rejected. Reason below. "
                        f"DO NOT REPEAT the same strategy.]\n\n{reason}"
                    ),
                    "internal": True,
                })
            # Dynamic delay before next attempt based on error type
            if last_record and last_record.error:
                if last_record.error == "fatal":
                    # A bad key or a rejected request will fail identically on
                    # every retry — stop instead of burning the turn.
                    logger.error("[AGENT] Fatal provider error, aborting.")
                    self._trace.finalize(None, AgentState.FAILED, time.time() - start_time)
                    self._trace.save()
                    return AgentResult(
                        success=False, output=None,
                        state=AgentState.FAILED.value,
                        iterations=last_record.iterations,
                        trace_id=trace_id,
                        error=f"Provider rejected the request ({last_record.error}). "
                              "Check the API key and model selection.",
                        model_used=primary_model, attempts=attempt_index - 1,
                        verification_status="failed",
                        verification_reason=f"provider_error:{last_record.error}",
                        attempt_status=AttemptStatus.RETRY.value,
                    )
                from agents.main.agent import _get_retry_delay
                delay = _get_retry_delay(last_record.error)
                if delay > 0:
                    logger.info(f"[AGENT] Waiting {delay:.1f}s before retry attempt {attempt_index}...")
                    time.sleep(delay)

            logger.info(
                f"\n{'='*70}\n"
                f"[AGENT] primary attempt {attempt_index}/{self.config.primary_attempts}, "
                f"model={primary_model}"
            )

            self._emit({
                "type": "attempt_started",
                "stage": "primary",
                "attempt": attempt_index,
                "model": primary_model,
            })

            attempt = self._attempt_once(
                llm=self.primary_llm, model_name=primary_model, attempt_index=attempt_index,
            )

            # A 429 is normally a per-minute throttle, and Google tells us
            # exactly how long to wait (`retryDelay`). Wait it out and retry the
            # same attempt instead of ending the turn, so the user's chat
            # survives an RPM limit. Only a per-day quota (or exhausted
            # retries) is a genuine dead end.
            rl_retries = 0
            rl_waited = 0.0
            max_rl_retries = max(1, self.config.rate_limit_retries)
            max_rl_wait = max(30.0, self.config.rate_limit_wait)
            while attempt.get("error") == "rate_limited":
                self._rate_limit_hits += 1
                if self._rate_limit_hits >= 2:
                    self._aggressive_trim = True
                    logger.info(
                        "[AGENT] Repeated rate limits (%d); enabling "
                        "aggressive context trimming for this session.",
                        self._rate_limit_hits,
                    )
                info = _parse_rate_limit(attempt.get("error_message"))
                if (
                    info is None or info.per_day
                    or rl_retries >= max_rl_retries
                    or rl_waited >= max_rl_wait
                ):
                    break
                wait = min(max(info.delay, 1.0) + 1.5, max_rl_wait - rl_waited)
                rl_retries += 1
                rl_waited += wait
                logger.info(
                    "[AGENT] Rate limited (RPM); waiting %.1fs then retrying "
                    "(retry %d/%d).", wait, rl_retries, max_rl_retries,
                )
                self._emit({
                    "type": "agent_message",
                    "message": f"Rate limit (RPM) — waiting {wait:.0f}s and continuing…",
                })
                time.sleep(wait)
                attempt = self._attempt_once(
                    llm=self.primary_llm, model_name=primary_model,
                    attempt_index=attempt_index,
                )

            if attempt.get("error") == "rate_limited":
                info = _parse_rate_limit(attempt.get("error_message"))
                per_day = bool(info and info.per_day)
                logger.info(
                    "[AGENT] Rate limited (%s), stopping.", "TPD" if per_day else "RPM"
                )
                record = AttemptRecord(
                    success=False, attempt_status="retry", aggregate="failed",
                    reasoning="rate_limited", output=None, state=attempt["state"],
                    iterations=attempt["iterations"], tool_calls_total=0,
                    tool_log=[], error="rate_limited", verification_status="failed",
                    verification_reason="rate_limited", model=primary_model,
                    attempt_index=attempt_index, duration=attempt["duration"],
                    error_message=attempt.get("error_message"),
                )
                self._trace.record(record)
                total_elapsed = time.time() - start_time
                self._trace.finalize(None, AgentState.FAILED, total_elapsed)
                self._trace.save()
                detail = (
                    "daily request limit exhausted"
                    if per_day
                    else "request limit not restored after several attempts"
                )
                return AgentResult(
                    success=False, output=None,
                    state=AgentState.FAILED.value,
                    iterations=attempt["iterations"],
                    trace_id=trace_id,
                    error=f"Request limit ({'TPD' if per_day else 'RPM'}): {detail}.",
                    model_used=primary_model, attempts=attempt_index,
                    verification_status="failed",
                    verification_reason="rate_limited",
                    attempt_status=AttemptStatus.RETRY.value,
                )

            decision = self.verifier.verify_attempt(
                registry=self.registry,
                attempt_state=attempt["state"],
                tool_calls_log=attempt["tool_log"],
                final_output=attempt["output"],
                provider_error=attempt.get("error"),
            )

            record = AttemptRecord(
                success=(decision.attempt_status == AttemptStatus.COMPLETED),
                attempt_status=decision.attempt_status.value,
                aggregate=decision.aggregate.value,
                reasoning=decision.reason,
                output=attempt["output"],
                state=attempt["state"],
                iterations=attempt["iterations"],
                tool_calls_total=attempt["tool_calls_total"],
                tool_log=decision.tool_verifications,
                error=attempt["error"],
                error_message=attempt.get("error_message"),
                verification_status=decision.aggregate.value,
                verification_reason=decision.reason,
                model=primary_model,
                attempt_index=attempt_index,
                duration=attempt["duration"],
            )

            self._trace.record(record)

            logger.info(
                f"[VERIFIER primary {attempt_index}] "
                f"decision={decision.attempt_status.value} "
                f"aggregate={decision.aggregate.value} "
                f"reason={decision.reason}"
            )

            self._emit({
                "type": "verification_decision",
                "stage": "primary",
                "attempt": attempt_index,
                "decision": decision.attempt_status.value,
                "aggregate": decision.aggregate.value,
                "reason": decision.reason,
            })

            if decision.attempt_status == AttemptStatus.COMPLETED:
                self.state = AgentState.FINISHED
                self._trace.verification_status = decision.aggregate.value
                self._trace.verification_reason = decision.reason
                total_elapsed = time.time() - start_time
                self._trace.finalize(attempt["output"], AgentState.FINISHED, total_elapsed)
                self._trace.save()
                return AgentResult(
                    success=True, output=attempt["output"],
                    state=AgentState.FINISHED.value,
                    iterations=attempt["iterations"],
                    tool_calls_total=attempt["tool_calls_total"],
                    trace_id=trace_id,
                    model_used=primary_model,
                    attempts=attempts_used,
                    verification_status=decision.aggregate.value,
                    verification_reason=decision.reason,
                    attempt_status=AttemptStatus.COMPLETED.value,
                )

            last_record = record

        self.state = AgentState.VERIFIED_FAILED
        total_elapsed = time.time() - start_time
        reason = last_record.verification_reason if last_record else "no_attempt"
        self._trace.verification_status = (
            last_record.aggregate if last_record else "unknown"
        )
        self._trace.verification_reason = reason
        self._trace.finalize(None, AgentState.VERIFIED_FAILED, total_elapsed)
        self._trace.save()
        # A provider outage (all attempts failed with the same transport error)
        # is not an answer-quality verdict: report the real cause, not "verifier
        # rejected".
        if last_record is not None and last_record.error and last_record.error_message:
            final_error = (
                f"Provider error ({last_record.error}): "
                f"{last_record.error_message}"
            )
        else:
            final_error = f"Verifier rejected after {attempts_used} attempts: {reason}"
        return AgentResult(
            success=False,
            output=None,
            state=AgentState.VERIFIED_FAILED.value,
            trace_id=trace_id,
            error=final_error,
            model_used=primary_model,
            attempts=attempts_used,
            verification_status=self._trace.verification_status,
            verification_reason=reason,
            attempt_status=AttemptStatus.RETRY.value,
        )

    def _attempt_once(
        self,
        llm: LLMClient,
        model_name: str,
        attempt_index: int,
    ) -> dict:
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

        tool_call_history: list[str] = []
        # Signature -> ToolResult for the whole turn, so a repeated call
        # replays its real output instead of being silently dropped.
        tool_result_cache: dict[str, ToolResult] = {}
        tool_log: list[dict] = []
        tool_calls_total = 0
        iter_count = 0
        empty_response_streak = 0
        MAX_EMPTY_RETRIES = 2

        start = time.time()
        try:
            for iteration in range(1, self.config.max_iterations + 1):
                iter_count = iteration
                self.state = AgentState.THINKING

                max_tokens = min(
                    self.context.calc_max_tokens(), self.config.max_output_tokens
                )
                messages = self.context.build(
                    provider=getattr(llm, "provider", None),
                    aggressive=self._aggressive_trim,
                )

                content = None
                tool_calls = None
                _finish = "stop"
                provider_error_message = None
                streamed_text = ""
                had_stream = False
                last_stream_emit = 0.0

                for kind, payload in llm.ask_stream(
                    messages,
                    tools=self._build_tools_for_llm(),
                    max_tokens=max_tokens,
                ):
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
                        # payload is (content, tool_calls, finish_reason,
                        # error_message); tolerate the older 3-tuple.
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
                if content is None and streamed_text:
                    content = streamed_text

                self._emit({
                    "type": "llm_responded",
                    "attempt": attempt_index,
                    "iteration": iteration,
                    "had_content": bool(content and content.strip()),
                    "tool_calls_count": len(tool_calls) if tool_calls else 0,
                    "thought_preview": (content[:200] if content else ""),
                })

                if content is None and tool_calls is None:
                    if _finish in ("rate_limited", "fatal", "server", "error"):
                        # Surface the real provider failure (e.g. the Gemini 503
                        # body) in the chat instead of hiding it behind silent
                        # retries that only end in a generic verifier message.
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
                            # The provider failed on this iteration. `out["output"]`
                            # still holds narration from an earlier iteration and
                            # must not be presented as this attempt's answer,
                            # otherwise the verifier judges a stale ghost.
                            "output": None,
                        })
                        return out
                    empty_response_streak += 1
                    logger.warning(f"[AGENT] empty response, streak={empty_response_streak}")
                    if empty_response_streak > MAX_EMPTY_RETRIES:
                        out.update({
                            "state": AgentState.FAILED.value,
                            "iterations": iteration,
                            "error": "LLM returned no response after retries",
                        })
                        return out
                    self.log.add_user(
                        "[System message: You did not answer the user. "
                        "Answer right now in English — briefly and to the point.]"
                    )
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
                                # Gemini 3 rejects functionCall parts replayed
                                # without the signature it handed us.
                                "thought_signature": getattr(
                                    tc.function, "thought_signature", None
                                ),
                            },
                        }
                        for tc in tool_calls
                    ]

                if tool_calls and self._detect_loop(tool_call_history, tc_list):
                    # Persist the assistant turn together with synthetic tool
                    # results so the conversation stays OpenAI-valid (an
                    # assistant message carrying tool_calls must be followed
                    # by matching tool messages). Otherwise the next attempt
                    # or provider rejects the replayed history.
                    self.log.add_assistant(content, tc_list)
                    for tc in tool_calls:
                        self.log.add_tool_result(
                            tc.id, tc.function.name,
                            "[not executed: loop pattern detected]",
                            result_status="error", duration=0.0,
                        )
                    out.update({
                        "state": AgentState.LOOP_EXCEEDED.value,
                        "iterations": iteration,
                        "error": "Loop pattern detected",
                        "tool_calls_total": tool_calls_total,
                        "tool_log": tool_log,
                        "duration": time.time() - start,
                    })
                    return out

                self.log.add_assistant(content, tc_list)

                if not tool_calls:
                    out.update({
                        "state": AgentState.FINISHED.value,
                        "iterations": iteration,
                        "output": content,
                        "tool_calls_total": tool_calls_total,
                        "tool_log": tool_log,
                        "duration": time.time() - start,
                    })
                    return out

                self.state = AgentState.EXECUTING
                # Narration is already delivered token-by-token via "stream"
                # events, so no extra emit here — that only duplicated text.
                # Tool-level narration arrives via the `narrate` argument.

                executed = self._execute_all(
                    tool_calls, tc_list, tool_call_history,
                    attempt_index=attempt_index,
                    sig_cache=tool_result_cache,
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

        except AgentLoopDetected as e:
            out.update({
                "state": AgentState.LOOP_EXCEEDED.value,
                "iterations": iter_count,
                "error": str(e),
                "tool_calls_total": tool_calls_total,
                "tool_log": tool_log,
                "duration": time.time() - start,
            })
            return out
        except AgentBudgetExceeded as e:
            out.update({
                "state": AgentState.BUDGET_EXCEEDED.value,
                "iterations": iter_count,
                "error": str(e),
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

    def _execute_all(self, tool_calls, tc_list: list, history: list[str], attempt_index: int = 0,
                     sig_cache: dict[str, ToolResult] | None = None) -> list[dict]:
        entries: list[dict] = []
        for tc, tc_info in zip(tool_calls, tc_list):
            call_id = tc_info["id"]
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}

            narrate = args.pop("narrate", None)
            if isinstance(narrate, str) and narrate.strip():
                self._emit({"type": "agent_message", "message": narrate.strip()})

            history.append(name)

            sig = f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"
            cached = sig_cache.get(sig) if sig_cache is not None else None
            if cached is not None:
                # Replaying the real previous result instead of a [SKIPPED]
                # stub. The old stub carried no data, so a model that repeated
                # a call — e.g. after the 150-char truncation hid the listing it
                # needed — got a dead end and kept calling until the loop cap.
                logger.info(
                    "[LOOP] Duplicate call replayed from cache: %s %s",
                    name, json.dumps(args, default=str)[:120],
                )
                tool_result = cached
                self._emit({
                    "type": "tool_started",
                    "tool": name,
                    "args": args,
                    "attempt": attempt_index,
                    "call_id": call_id,
                })
                self._emit({
                    "type": "tool_finished",
                    "tool": name,
                    "status": getattr(tool_result, "status", "success"),
                    "duration": 0.0,
                    "cached": True,
                    "attempt": attempt_index,
                    "call_id": call_id,
                })
                self.log.add_tool_result(
                    tc.id, name, tool_result.to_text(), tool_result.status, 0.0,
                    summary=tool_result.summary,
                )
                entries.append({
                    "call_id": call_id,
                    "tool": name,
                    "args": args,
                    "result": tool_result,
                })
                continue

            self._emit({
                "type": "tool_started",
                "tool": name,
                "args": args,
                "attempt": attempt_index,
                "call_id": call_id,
            })

            exec_start = time.time()
            try:
                tool_result: ToolResult = self._execute_tool(name, args, call_id)
            except Exception as e:
                # One broken tool must never abort the whole turn.
                logger.warning(f"[AGENT] Tool '{name}' raised {type(e).__name__}: {e}")
                available = ", ".join(sorted(getattr(self.registry, "_tools", {})))
                tool_result = ToolResult(
                    status="error",
                    error=f"{type(e).__name__}: {e}. Available tools: {available}",
                )
            tool_result.execution_time = time.time() - exec_start

            if sig_cache is not None:
                sig_cache[sig] = tool_result

            # Surface plan updates to the UI immediately.
            if name == "update_plan" and tool_result.success:
                self._emit({
                    "type": "plan_updated",
                    "plan": (tool_result.data or {}).get("steps"),
                })

            # Lazy-load tool schema from get_tools execution
            if name == "get_tools" and tool_result.success:
                try:
                    data = tool_result.data or {}
                    schemas = data.get("schemas")
                    if isinstance(schemas, dict):
                        names = list(schemas.keys())
                    else:
                        raw = data.get("tool_name", args.get("tool_name", ""))
                        names = [raw] if isinstance(raw, str) else list(raw or [])
                    for loaded_name in names:
                        if loaded_name and loaded_name in self.registry._tools:
                            self._loaded_tool_names.add(loaded_name)
                            logger.info(f"[AGENT] Loaded full schema for tool: {loaded_name}")
                except Exception:
                    logger.debug("[AGENT] get_tools schema load failed", exc_info=True)

            self._emit({
                "type": "tool_finished",
                "tool": name,
                "status": tool_result.status,
                "duration": round(tool_result.execution_time, 3),
                "attempt": attempt_index,
                "call_id": call_id,
            })

            result_text = self._format_result(tool_result)
            self.log.add_tool_result(
                tc.id, name, result_text,
                tool_result.status, tool_result.execution_time,
                summary=tool_result.summary,
            )

            entries.append({
                "call_id": call_id,
                "tool": name,
                "args": args,
                "result": tool_result,
            })

        return entries

    def _format_result(self, result: ToolResult) -> str:
        text = result.to_text()
        if len(text) > TOOL_OUTPUT_LIMIT:
            text = text[:TOOL_OUTPUT_LIMIT] + f"\n...[truncated, {len(text)} chars]"
        return text

    def _execute_tool(self, name: str, args: dict, call_id: str) -> ToolResult:
        """Run a tool, wiring the progress hook when the registry takes one.

        `CodeToolRegistry` forwards `on_progress` to tools that declare it; the
        plain `ToolRegistry` has no such parameter, so the call degrades to the
        old two-argument form instead of raising.
        """
        execute = self.registry.execute
        try:
            takes_progress = "on_progress" in inspect.signature(execute).parameters
        except (TypeError, ValueError):
            takes_progress = False
        if takes_progress:
            return execute(name, args, on_progress=self._progress_reporter(name, call_id))
        return execute(name, args)

    def _progress_reporter(self, tool_name: str, call_id: str):
        """Build the `on_progress` hook a long-running tool can call.

        Only tools that declare the parameter get one, and it is called from the
        native worker threads, not from the agent thread: `_emit` hands the event
        to the SSE layer, which pushes it onto a bounded bus - never onto client
        state directly, and never blocking, so the walk is never stalled by a
        slow reader.
        """
        def report(sample: dict) -> None:
            self._emit({
                "type": "tool_progress",
                "tool": tool_name,
                "call_id": call_id,
                "percent": round(float(sample.get("percent") or 0.0), 1),
                "nodes": int(sample.get("nodes") or 0),
                "bytes": int(sample.get("bytes") or 0),
                "queued": int(sample.get("queued") or 0),
                "elapsed": round(float(sample.get("elapsed") or 0.0), 2),
                "done": bool(sample.get("done")),
            })

        return report

    def _detect_loop(self, history: list[str], current: list[dict]) -> bool:
        if len(history) < 4:
            return False

        names = [tc["function"]["name"] for tc in current]
        if not names:
            return False

        # shell repeated 4+ times is NOT a loop — it's normal for multi-step tasks
        if all(n == names[0] for n in history[-4:]):
            if names[0] != "shell":
                logger.warning(f"[LOOP] Same tool repeated 4+ times: {names[0]}")
                return True

        if len(history) >= 6:
            last_6 = history[-6:]
            if (last_6[0] == last_6[2] == last_6[4]
                and last_6[1] == last_6[3] == last_6[5]):
                logger.warning(f"[LOOP] ABAB pattern detected: {' → '.join(last_6)}")
                return True

        if len(history) >= 9:
            last_9 = history[-9:]
            if (last_9[0] == last_9[3] == last_9[6]
                and last_9[1] == last_9[4] == last_9[7]
                and last_9[2] == last_9[5] == last_9[8]):
                logger.warning(f"[LOOP] ABCABC pattern detected")
                return True

        return False

    def reset(self):
        self.log = EventLog()
        self.context = self._make_context(self.system_prompt)
        self.state = AgentState.IDLE
        self._trace = None
