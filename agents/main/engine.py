"""The single agent engine, shared by the main chat and the code agent.

Shared types, defaults and constants live in `agent.py` so there is exactly
one `AgentConfig`, one `AgentResult` and one set of loop limits. This module
only orchestrates a turn.
"""

import logging
import time
import uuid

from shared.llm_client import LLMClient
from shared.cancellation import AgentCancelled, CancelToken, sleep_interruptible
from .history import EventLog, ContextBuilder
from .tools.registry import ToolRegistry, AttemptStatus
from .rate_limiter import RateLimiter, get_retry_delay, parse_rate_limit
from .attempt_executor import AttemptExecutor
from .trace_recorder import ExecutionTrace, build_retry_reason
from .agent import AgentState, AgentConfig, AgentResult, AttemptRecord

logger = logging.getLogger("agent")


class Agent:
    def __init__(
        self,
        registry: ToolRegistry,
        primary_llm: LLMClient,
        system_prompt: str = "",
        config: AgentConfig | None = None,
        event_sink=None,
        plan_store=None,
        plan_key: str | None = None,
    ):
        self.registry = registry
        self.primary_llm = primary_llm
        # Injected by the code factory so the engine's live plan reminder and the
        # update_plan tool read the same store. The main chat has no plan store.
        self.plan_store = plan_store
        self.plan_key = plan_key
        self.system_prompt = self._build_prompt_with_catalog(system_prompt)
        self.config = config or AgentConfig()
        self._event_sink = event_sink

        # Components. AttemptExecutor owns the per-turn ToolExecutor; keeping a
        # second one here meant set_event_sink updated an instance that never ran.
        self.rate_limiter = RateLimiter(self.config)

        # State
        self.log = EventLog()
        self.context = ContextBuilder(
            self.log, self.system_prompt, max_tokens=self.config.max_context_tokens,
        )
        self.state = AgentState.IDLE
        self._trace: ExecutionTrace | None = None
        self._loaded_tool_names: set[str] = set()
        # Cancellation flag for the current turn, set by the route from the
        # shared registry so /agent/stop and a dropped client can both stop it.
        self._cancel_token: CancelToken | None = None
        # Loop nudges survive retries: a model that ignored the warning in
        # attempt 1 must not get a fresh set of warnings in attempt 2.
        self._loop_nudges = 0

    def set_cancel_token(self, token: CancelToken | None):
        """Point the agent at the current turn's cancellation flag."""
        self._cancel_token = token

    def _finish_cancelled(
        self, trace_id: str, primary_model: str, start_time: float,
        iterations: int = 0, tool_calls_total: int = 0, attempts_used: int = 0,
    ) -> AgentResult:
        """Unwind a stopped turn into a normal, honest result."""
        self.state = AgentState.CANCELLED
        self._trace.verification_status = "cancelled"
        self._trace.verification_reason = "user_cancelled"
        self._trace.finalize(None, AgentState.CANCELLED, time.time() - start_time)
        self._trace.save()
        return AgentResult(
            success=False, output=None,
            state=AgentState.CANCELLED.value,
            iterations=iterations,
            tool_calls_total=tool_calls_total,
            trace_id=trace_id,
            error="Остановлено пользователем",
            model_used=primary_model,
            attempts=attempts_used,
            verification_status="cancelled",
            verification_reason="user_cancelled",
            attempt_status=AttemptStatus.COMPLETED.value,
        )

    def _build_prompt_with_catalog(self, base_prompt: str) -> str:
        """Catalog is sent via tools= parameter; keep system prompt lean."""
        return base_prompt

    def _build_tools_for_llm(self) -> list[dict]:
        """Live tool schemas for the planning path (ExecutionManager)."""
        return self.registry.get_llm_specs()

    def _make_context(self, system_prompt: str) -> ContextBuilder:
        return ContextBuilder(
            self.log, system_prompt, max_tokens=self.config.max_context_tokens,
        )

    def set_event_sink(self, sink):
        """Re-point streaming events at the current request's sink."""
        self._event_sink = sink

    def load_history(self, events: list[dict]):
        """Seed this agent with a previously persisted conversation."""
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
        # Warning budget is per user turn, not per attempt (see AttemptExecutor).
        self._loop_nudges = 0

        self.registry.reset_all()

        for attempt_index in range(1, self.config.primary_attempts + 1):
            # Stop before spending another provider call on a turn the user
            # already cancelled.
            if self._cancel_token is not None and self._cancel_token.cancelled:
                return self._finish_cancelled(
                    trace_id, primary_model, start_time, attempts_used=max(0, attempt_index - 1),
                )
            # Overall safety net: one attempt already has its own deadline.
            if attempt_index > 1 and time.time() - start_time > (
                self.config.max_execution_time * max(1, self.config.primary_attempts)
            ):
                logger.warning("[AGENT] overall time budget exhausted, skipping retry")
                break
            # Retrying after a budget exhaustion cannot help: the counters are
            # per turn now, so the second attempt would start with zero room.
            if last_record is not None and (
                last_record.iterations >= self.config.max_iterations
                or last_record.tool_calls_total >= self.config.max_tool_calls
            ):
                logger.warning("[AGENT] turn budget exhausted, skipping retry")
                break
            attempts_used = attempt_index

            # Claude Code-style retry: continue the same conversation. The
            # failed calls and their results are already in the transcript, so
            # the model can see what went wrong. Injecting a synthetic "user"
            # retry report used to pollute long-term memory and made the model
            # answer the note instead of the original request.
            user_input_for_attempt = user_input if attempt_index == 1 else ""
            if last_record is not None:
                logger.info(
                    "[AGENT] continuing after failed attempt %d: %s",
                    attempt_index - 1, build_retry_reason(last_record),
                )

            # Dynamic delay before next attempt based on error type
            if last_record is not None and last_record.error:
                if last_record.error == "fatal":
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
                delay = get_retry_delay(last_record.error)
                if delay > 0:
                    logger.info(f"[AGENT] Waiting {delay:.1f}s before retry attempt {attempt_index}...")
                    try:
                        sleep_interruptible(delay, self._cancel_token)
                    except AgentCancelled:
                        return self._finish_cancelled(
                            trace_id, primary_model, start_time,
                            attempts_used=attempt_index - 1,
                        )

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

            # One executor for the whole turn, sharing the agent's event log and
            # context. A fresh executor used to bring its own empty EventLog, so
            # tool results were never replayed to the model and the persisted
            # conversation (agent.log) stayed empty.
            executor = AttemptExecutor(
                self.registry, self.system_prompt, self.config, self._event_sink,
                getattr(self.primary_llm, "provider", None),
                event_log=self.log,
                context=self.context,
                cancel_token=self._cancel_token,
                loop_nudges=self._loop_nudges,
                iterations_used=last_record.iterations if last_record else 0,
                tool_calls_used=last_record.tool_calls_total if last_record else 0,
                plan_store=self.plan_store,
                plan_key=self.plan_key,
            )

            # Execute attempt
            attempt = executor.execute_attempt(
                llm=self.primary_llm,
                model_name=primary_model,
                user_input=user_input_for_attempt,
                loaded_tool_names=self._loaded_tool_names,
                attempt_index=attempt_index,
                aggressive_trim=self.rate_limiter.aggressive_trim,
                user_internal=False,
            )
            self._loop_nudges = executor.loop_nudges

            # Handle rate limit on the same executor, so a retry continues the
            # conversation instead of starting from an empty log.
            attempt = executor.handle_rate_limit(
                attempt, self.primary_llm, primary_model,
                self._loaded_tool_names, attempt_index
            )
            self._loop_nudges = executor.loop_nudges

            if attempt.get("error") == "rate_limited":
                info = parse_rate_limit(attempt.get("error_message"))
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

            # Claude Code-style: the model decides when the task is done.
            # If it produced text without tool calls, accept it as the final
            # answer. If it errored, report the error. No external verifier,
            # no retry counter, no "Verifier rejected" dead ends.
            state = attempt.get("state", AgentState.FAILED.value)
            output = attempt.get("output")
            error = attempt.get("error")
            error_message = attempt.get("error_message")
            tool_calls_total = attempt.get("tool_calls_total", 0)
            iterations = attempt.get("iterations", 0)
            duration = attempt.get("duration", 0.0)

            if state == AgentState.CANCELLED.value:
                return self._finish_cancelled(
                    trace_id, primary_model, start_time,
                    iterations=iterations, tool_calls_total=tool_calls_total,
                    attempts_used=attempts_used,
                )

            if state == AgentState.FINISHED.value:
                self.state = AgentState.FINISHED
                # Record the successful attempt as well. Only failures used to
                # reach the trace, so a finished turn was saved with a stale
                # "failed" attempt and the previous attempt's tool-call count.
                self._trace.record(AttemptRecord(
                    success=True,
                    attempt_status=AttemptStatus.COMPLETED.value,
                    aggregate="success", reasoning=state, output=output,
                    state=state, iterations=iterations,
                    tool_calls_total=tool_calls_total,
                    tool_log=attempt.get("tool_log", []), error=error,
                    verification_status="success",
                    verification_reason="model_decided_done",
                    model=primary_model, attempt_index=attempt_index,
                    duration=duration, error_message=error_message,
                ))
                self._trace.verification_status = "success"
                self._trace.verification_reason = "model_decided_done"
                total_elapsed = time.time() - start_time
                self._trace.finalize(output, AgentState.FINISHED, total_elapsed)
                self._trace.save()
                return AgentResult(
                    success=True, output=output,
                    state=AgentState.FINISHED.value,
                    iterations=iterations,
                    tool_calls_total=tool_calls_total,
                    trace_id=trace_id,
                    model_used=primary_model,
                    attempts=attempts_used,
                    verification_status="success",
                    verification_reason="model_decided_done",
                    attempt_status=AttemptStatus.COMPLETED.value,
                )

            last_record = AttemptRecord(
                success=False, attempt_status="retry", aggregate="failed",
                reasoning=state, output=output, state=state,
                iterations=iterations, tool_calls_total=tool_calls_total,
                tool_log=attempt.get("tool_log", []), error=error,
                verification_status="failed",
                verification_reason=state, model=primary_model,
                attempt_index=attempt_index, duration=duration,
                error_message=error_message,
            )
            self._trace.record(last_record)

        self.state = AgentState.FAILED
        total_elapsed = time.time() - start_time
        reason = last_record.verification_reason if last_record else "no_attempt"
        err_msg = last_record.error_message if last_record else None
        self._trace.verification_status = "failed"
        self._trace.verification_reason = reason
        self._trace.finalize(None, AgentState.FAILED, total_elapsed)
        self._trace.save()
        # The reason alone ("loop_exceeded") says nothing about what actually
        # went wrong. The attempt executor puts the detail in `error`
        # ("Loop pattern detected", "tool_call_budget_exhausted(16)"), and
        # `error_message` carries provider text. Surface whichever is available.
        detail = err_msg or (last_record.error if last_record else None)
        if detail and detail != reason:
            final_error = f"Agent failed after {attempts_used} attempt(s): {reason} — {detail}"
        elif detail:
            final_error = detail
        else:
            final_error = f"Agent failed after {attempts_used} attempt(s): {reason}"
        return AgentResult(
            success=False,
            output=None,
            state=AgentState.FAILED.value,
            trace_id=trace_id,
            error=final_error,
            model_used=primary_model,
            attempts=attempts_used,
            verification_status="failed",
            verification_reason=reason,
            attempt_status=AttemptStatus.RETRY.value,
        )

    def reset(self):
        self.log = EventLog()
        self.context = self._make_context(self.system_prompt)
        self.state = AgentState.IDLE
        self._trace = None
        self._loaded_tool_names.clear()
        self._loop_nudges = 0
        self.rate_limiter = RateLimiter(self.config)