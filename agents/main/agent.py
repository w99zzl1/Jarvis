"""Shared types, limits and constants for the single agent engine.

The `Agent` class itself lives in `engine.py`; importing it from here still
works through the lazy alias at the bottom so older call sites keep working.
Keeping only the shared definitions here avoids a second copy of the trace,
rate-limit and retry code drifting away from the engine that actually runs.
"""
from dataclasses import dataclass, asdict
from enum import Enum


# Claude Code does not cap tool calls; it lets the model work until the task is
# done and relies on the context window plus the user's interrupt. These are
# deliberately generous safety rails, not a work budget: the old 40/80 killed a
# repo-wide task before it even started.
MAX_ITERATIONS_DEFAULT = 200
MAX_TOOL_CALLS_DEFAULT = 400
# A safety net against a runaway turn, not a work budget: a repo-wide task took
# 58s just to read the project, so a 60s ceiling would have re-introduced the
# very failure the raised tool-call limits fixed.
MAX_EXECUTION_TIME_DEFAULT = 600

# Ceiling for the doubled retry budget when a provider truncates a response.
# Generous enough for a thinking model to finish its visible answer, bounded so
# a runaway generation cannot bill indefinitely.
MAX_OUTPUT_BUDGET = 16384

# A detected repetition is first reported to the model as an instruction to
# change approach, not as a failure. Only after the model ignores this note
# twice does the turn end with `loop_exceeded`. Internal: never shown as a user
# message in the history drawer, but it *is* replayed to the model so it can
# course-correct.
LOOP_NUDGE_NOTE = (
    "[Системное сообщение: Не повторяйся, смени тактику. "
    "Ты снова вызываешь те же инструменты с теми же аргументами. "
    "Остановись и выбери другой подход: прочитай другой файл, измени "
    "аргументы, либо сразу сообщи пользователю результат. "
    "Не повторяй невыполненные вызовы. Отвечай на языке пользователя.]"
)

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
    CANCELLED = "cancelled"


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
    # Output budget per LLM call. This is a ceiling, not a target: the prompt
    # keeps spoken answers short, but a reasoning model (gpt-oss, o-series)
    # spends part of the budget on hidden reasoning, and a tool call's arguments
    # can be long. At 600 the provider cut the arguments mid-string and rejected
    # them as invalid JSON, so a tool-calling turn needs real headroom. The
    # provider clamps this down to fit its own token window (see
    # BaseProvider._cap_max_tokens), so a small free-tier TPM is still honoured.
    max_output_tokens: int = 4096
    # How long one attempt may spend idling on a per-minute throttle. The main
    # agent answers in a few calls, so a short leash keeps chat responsive; a
    # code agent may need dozens of calls, and free tiers only allow 5-30 RPM.
    rate_limit_retries: int = MAX_RATE_LIMIT_RETRIES
    rate_limit_wait: float = MAX_RATE_LIMIT_WAIT
    # A repeated tool-call pattern is no longer an instant failure: the model
    # gets this many "change tactics" notes before the turn is aborted.
    loop_nudges_before_abort: int = 2


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


def __getattr__(name):
    if name == "Agent":
        from .engine import Agent as _Agent
        return _Agent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | {"Agent"})
