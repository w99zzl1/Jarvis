"""Execution trace recording for the agent."""

import json
import logging
import os
import time
import uuid
from dataclasses import asdict
from typing import Any

from .agent import AgentState, AgentResult, AgentConfig

logger = logging.getLogger("agent.trace")


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

    def record(self, ar: Any):
        """Record an attempt record."""
        try:
            self.attempts.append(asdict(ar))
            self.total_tool_calls += ar.tool_calls_total
        except Exception as e:
            logger.error(f"[TRACE] Failed to record attempt: {e}", exc_info=True)
            # Fallback: store raw dict representation
            try:
                self.attempts.append(dict(ar) if hasattr(ar, '__iter__') else {"error": str(ar)})
            except Exception:
                self.attempts.append({"error": f"Failed to serialize attempt: {e}"})

    def finalize(self, output: str | None, state: AgentState, total_time: float):
        self.final_output = output
        # Older code paths occasionally handed a plain string here; accepting
        # both keeps a finished turn from dying in the trace recorder.
        self.final_state = getattr(state, "value", state)
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


def build_retry_reason(prev_ar: Any) -> str:
    lines = [
        "Previous attempt did not complete successfully.",
        "",
        f"Model             : {prev_ar.model}",
        f"Agent state       : {prev_ar.state}",
        f"Reason            : {prev_ar.reasoning}",
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
    lines.append("Do not repeat the same strategy. Continue in the user's language.")
    return "\n".join(lines)