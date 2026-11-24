"""Backward-compatible import path for the single agent engine.

The refactored implementation moved to `engine.py`; shared types and limits
live in `agent.py`. Both the main chat and the code agent now use the same
engine, so a fix only has to be made once.
"""

from .agent import (
    AgentConfig,
    AgentResult,
    AgentState,
    AttemptRecord,
    AgentLoopDetected,
    AgentBudgetExceeded,
    MAX_ITERATIONS_DEFAULT,
    MAX_TOOL_CALLS_DEFAULT,
    MAX_EXECUTION_TIME_DEFAULT,
)
from .engine import Agent

__all__ = [
    "Agent",
    "AgentConfig",
    "AgentResult",
    "AgentState",
    "AttemptRecord",
    "AgentLoopDetected",
    "AgentBudgetExceeded",
    "MAX_ITERATIONS_DEFAULT",
    "MAX_TOOL_CALLS_DEFAULT",
    "MAX_EXECUTION_TIME_DEFAULT",
]
