"""Verifier — structural invariant-based aggregator.

Decides COMPLETED / RETRY from a small set of structural signals:

1. tool_calls_made       — did the model emit OpenAI-style tool_calls?
2. stripped_output       — final output with any JSON status block stripped
3. pseudo_tool_attempt   — does the text look like the model tried to call
                           a tool by writing JSON/function syntax inline?

Hard invariants (NOT heuristics we add one by one):

  I1. If model emitted native tool_calls, look at tool verify() results.
      Tool-level decision wins (already split into SUCCESS/FAILED/UNKNOWN).

  I2. If model emitted ZERO native tool_calls AND THE TEXT contains a
      pseudo-tool-call attempt, this MUST be RETRY. The model did NOT do
      its job; it wrote JSON instead of using the tool-calling API.

  I3. If model emitted ZERO native tool_calls AND the user-visible output
      is empty, the model is "done" but produced no answer. This MUST
      be RETRY.

Everything else (no tool_calls, text-only reply) is the normal flow and
counts as COMPLETED.

The verifier never executes tools. It reads ToolResult and Tool.verify().
"""
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from .tools.registry import (
    ToolRegistry, ToolResult, VerificationStatus, AttemptStatus,
)

logger = logging.getLogger("verifier")


_STATUS_PATTERNS = (
    re.compile(
        r"\{[^{}]*\"status\"\s*:\s*\""
        r"(completed|cannot_complete|partial|failed)\"[^{}]*\}"
    ),
    re.compile(
        r"```json\s*\{[^{}]*\"status\"\s*:\s*\""
        r"(completed|cannot_complete|partial|failed)\"[^{}]*\}\s*```"
    ),
)
_KNOWN_VALID_STATUSES = {"completed", "cannot_complete", "partial", "failed"}


def parse_structured_status(text: str | None) -> str | None:
    """Parse a `{"status":"..."}` JSON block. Returns the value or None.

    Structural, not keyword-based.
    """
    if not text:
        return None
    for pat in _STATUS_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1).lower()
    try:
        m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
        if m:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, dict) and "status" in parsed:
                s = str(parsed["status"]).lower()
                if s in _KNOWN_VALID_STATUSES:
                    return s
    except Exception:
        pass
    return None


_STRIP_STATUS_RE = re.compile(
    r"```json\s*\{.*\"status\"\s*:.*?\}\s*```"
    r"|"
    r"\{\s*\"status\"\s*:\s*\"(?:completed|cannot_complete|partial|failed)\".*?\}"
)


def strip_status_block(text: str | None) -> str | None:
    """Remove `{"status":"..."}` blocks and return what remains.

    Returns None when the cleaned text is empty.
    """
    if not text:
        return None
    cleaned = _STRIP_STATUS_RE.sub("", text).strip()
    return cleaned or None


# ---------------------------------------------------------------------------
# Pseudo-tool-attempt detection
#
# The model emits text like:
#     powershell>{"script": "..."}
#     <cmd>{"command":"dir"}</cmd>
#     <function/cmd>{"command":"time /t"}
#     ```powershell {"script":"..."}```
#     {"script": "Get-Date"}     ← bare JSON with a tool-shaped field
#
# The detector below catches those through TWO independent structural signals,
# not a long list of regexes:
#   (a) balanced HTML-ish tags whose name is a known tool AND whose body
#       is JSON-like.
#   (b) key:value JSON-shaped fragments where the key is one of the known
#       PowerShell / cmd argument fields and the value is a string.
# ---------------------------------------------------------------------------

_KNOWN_TOOL_NAMES = {
    "shell", "cmd", "powershell", "run_cmd", "run_powershell",
    "clip", "get_screenshot", "get_system_info",
    "search_web", "get_location", "monitor", "memory",
    "python", "browser", "vision", "mouse_keyboard",
}

_TOOL_ARG_KEYS = {"script", "command", "text", "args", "url", "selector", "action", "path", "key", "value", "query"}

_PSEUDO_TOOL_TAG = re.compile(
    r"<\s*(?P<tag>%s)\b[^>]*>(?P<body>[^<\n]*?[\{\"]+[^<\n]*?)<\s*/\s*(?P=tag)\s*>"
    % "|".join(re.escape(n) for n in _KNOWN_TOOL_NAMES),
    re.IGNORECASE | re.DOTALL,
)

_PSEUDO_TOOL_NO_CLOSE = re.compile(
    r"<\s*(?P<tag>%s)\b[^/>]*>"
    % "|".join(re.escape(n) for n in _KNOWN_TOOL_NAMES),
    re.IGNORECASE,
)

_PSEUDO_BARE_SHELL = re.compile(
    r"(?:^|\s|```)(?P<tag>powershell|cmd)\s*>\s*\{",
    re.IGNORECASE,
)

_PSEUDO_FENCED_SHELL = re.compile(
    r"```\s*(?P<tag>powershell|cmd)\b",
    re.IGNORECASE,
)

# Text-form tool calls like "memory save key=x value=y", "memory search
# \"любимый цвет\"", "shell command=Get-Date" — the model wrote a tool
# invocation as prose instead of using native function_calling.
_PSEUDO_TEXT_TOOL_CALL = re.compile(
    r"^\s*(?:tool\s*:?\s*)?(?P<tool>shell|run_cmd|run_powershell|memory|clip|search_web|monitor|task_status)\b[^\n]*?"
    r"(?:\b(?:key|value|action|query|command|text|name|task_id)\s*=|"
    r"[\"']|"
    r"\b(?:save|search|delete|start|stop|get|list|cancel)\b)",
    re.IGNORECASE | re.MULTILINE,
)

_INFERRED_KEY_VAL = re.compile(
    r"\{\s*\"(?P<key>%s)\"\s*:\s*\"[^\"]+\"\s*[,}]"
    % "|".join(re.escape(k) for k in _TOOL_ARG_KEYS),
    re.IGNORECASE,
)

# Gemini leaking its private tool syntax into prose, e.g.
# "default_api:read_file{path:memory_store.json}" or a bare
# "Tool calls: read_file{path=...}" that never reached the tool layer.
_PSEUDO_GEMINI_PRIVATE_API = re.compile(
    r"\bdefault_api\s*:",
    re.IGNORECASE,
)

# Matches any `name{...}` / `name{...}` / `name(key=value)` call written as
# prose, regardless of whether the tool is one we know about.
_PSEUDO_NAMED_CALL_BRACES = re.compile(
    r"\btools?\s*calls?\s*:\s*"
    r"(?:[a-z_][a-z0-9_]*\s*:\s*)?"
    r"[a-z_][a-z0-9_]*\s*(?:\[[^\]]*\])?\s*(?:\{[^{}]*\}|\([^()]*\))",
    re.IGNORECASE,
)


def _detect_pseudo_tool_attempt(text: str | None) -> bool:
    """True if the text looks like a model-written tool invocation.

    Whitelisted queries like plain `{"command": "..."}` in user input must NOT
    trigger this, but at the verifier stage we are looking at the model's
    own response, not the user's input. The structural signals below are
    biased toward 'model output looks like an attempt to call a tool'.
    """
    if not text:
        return False
    if _PSEUDO_TOOL_TAG.search(text):
        return True
    if _PSEUDO_TOOL_NO_CLOSE.search(text):
        return True
    if _PSEUDO_BARE_SHELL.search(text):
        return True
    if _PSEUDO_FENCED_SHELL.search(text):
        return True
    if _PSEUDO_TEXT_TOOL_CALL.search(text):
        return True
    if _PSEUDO_GEMINI_PRIVATE_API.search(text):
        return True
    if _PSEUDO_NAMED_CALL_BRACES.search(text):
        return True
    if "<function" in text.lower():
        return True
    if "{" in text and "}" in text and '"command"' in text:
        return True
    if "<tool_call" in text.lower():
        return True
    return False

# Tool names that change state on disk. Used to decide whether a turn that
# opened a plan but never closed it actually did the work.
_MUTATING_TOOL_HINTS = (
    "write", "edit", "create", "delete", "remove", "move", "rename",
    "copy", "append", "patch", "shell", "run", "exec",
)


def _is_mutating_tool(name: str) -> bool:
    lowered = (name or "").lower()
    return any(hint in lowered for hint in _MUTATING_TOOL_HINTS)


def _plan_progress(tool_calls_log: list[dict]) -> tuple[int, int, bool] | None:
    """Summarize the last `update_plan` call: (completed, total, work_after).

    `update_plan` is a legitimate tool whose result is always "success", so a
    turn that only planned - text reply, plan stored, zero files written - used
    to pass verification as `all_tools_verified_success(1/1)`. The user then saw
    "done" for work that never happened.

    `work_after` means a state-changing tool ran successfully *after* that last
    plan update, which is the signal that the plan's open steps are just stale
    bookkeeping rather than unfinished work.
    """
    last_index = -1
    steps: list | None = None
    for index, entry in enumerate(tool_calls_log):
        name = entry.get("tool") or entry.get("name") or ""
        if name != "update_plan":
            continue
        data = getattr(entry.get("result"), "data", None)
        if isinstance(data, dict) and isinstance(data.get("steps"), list):
            last_index = index
            steps = data["steps"]
    if steps is None:
        return None

    completed = sum(
        1 for step in steps
        if isinstance(step, dict) and step.get("status") == "completed"
    )
    work_after = any(
        _is_mutating_tool(entry.get("tool") or entry.get("name") or "")
        and getattr(entry.get("result"), "status", "") == "success"
        for entry in tool_calls_log[last_index + 1:]
    )
    return completed, len(steps), work_after


# Narration without tools: the model says "I'll create X" but never called a
# tool. These patterns indicate future-tense prose about work that hasn't
# happened yet — retry and let the model actually call tools.
_NARRATION_PATTERNS = (
    r"\bi\s*(will|'ll|gonna)\s+(create|write|make|add|edit|update|delete|remove|generate|build)\b",
    r"\bi\s*(will|'ll|gonna)\s+(set up|put together|come up with)\b",
    r"\bhere'?s\s+what\s+i\s*(will|'ll)\s+do\b",
    r"\blet\s+me\s+(create|write|make|add|edit|update|delete|generate|build)\b",
    r"\bi\s+can\s+(create|write|make|add|edit|generate|build)\b",
)


def _looks_like_narration(text: str | None) -> bool:
    """True if the model's text looks like narrating future work instead of
    reporting completed tool results.

    Used in the text-only verifier path: if the model says "I'll create
    storage.py" without ever calling write_file, it's not done — it's
    procrastinating. Retry and let the model call tools.
    """
    if not text:
        return False
    lowered = text.lower()
    return any(re.search(pat, lowered) for pat in _NARRATION_PATTERNS)



# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


@dataclass
class AttemptDecision:
    attempt_status: AttemptStatus
    tool_verifications: list[dict]
    aggregate: VerificationStatus
    reason: str
    fatal_agent_state: bool = False

    def to_dict(self) -> dict:
        return {
            "attempt_status": self.attempt_status.value,
            "aggregate": self.aggregate.value,
            "reason": self.reason,
            "fatal_agent_state": self.fatal_agent_state,
            "tool_verifications": self.tool_verifications,
        }


class Verifier:
    """Read-only aggregator. No side-effects."""

    def verify_attempt(
        self,
        registry: ToolRegistry,
        attempt_state: str,
        tool_calls_log: list[dict],
        final_output: str | None = None,
        provider_error: str | None = None,
    ) -> AttemptDecision:
        fatal_state = attempt_state in {"failed", "loop_exceeded", "budget_exceeded"}

        # ---------- Transport failures ----------
        # A provider error says nothing about the quality of an answer, so it
        # must never be reported as an answer-quality verdict. Without this the
        # retry reason read `text_only_reply_present` for what was really a
        # Gemini `503 UNAVAILABLE`, blaming the model for a capacity limit.
        if provider_error:
            retryable = provider_error not in {"fatal"}
            logger.info(
                "[VERIFIER] provider_error=%s (%s)",
                provider_error, "retryable" if retryable else "permanent",
            )
            return AttemptDecision(
                attempt_status=AttemptStatus.RETRY,
                tool_verifications=[],
                aggregate=VerificationStatus.FAILED,
                reason=f"provider_error:{provider_error}",
                fatal_agent_state=False,
            )

        explicit_status = parse_structured_status(final_output)
        stripped = strip_status_block(final_output)
        pseudo = _detect_pseudo_tool_attempt(final_output)
        native_calls = bool(tool_calls_log)

        # ---------- Tool-level path ----------
        if native_calls:
            tool_verifications: list[dict] = []
            for entry in tool_calls_log:
                tool_name = entry.get("tool") or entry.get("name") or ""
                # Skip get_tools since it is a meta tool that doesn't change system state
                if tool_name == "get_tools":
                    continue
                args = entry.get("args") or entry.get("arguments") or {}
                result = entry.get("result")
                if not tool_name or result is None:
                    tool_verifications.append({
                        "tool": tool_name,
                        "args": args,
                        "result_status": "?",
                        "verification": "unknown",
                    })
                    continue
                try:
                    v_status = registry.verify(tool_name, args, result)
                except Exception as e:
                    logger.warning(f"[VERIFIER] tool '{tool_name}' raised in verify(): {e}")
                    v_status = VerificationStatus.UNKNOWN
                tool_verifications.append({
                    "tool": tool_name,
                    "args": args,
                    "result_status": getattr(result, "status", "?"),
                    "verification": v_status.value,
                })

            s_count = sum(1 for v in tool_verifications if v.get("verification") == "success")
            f_count = sum(1 for v in tool_verifications if v.get("verification") == "failed")
            u_count = sum(1 for v in tool_verifications if v.get("verification") == "unknown")

            if f_count > 0:
                agg = VerificationStatus.FAILED
                by = "at_least_one_tool_failed"
                reason = f"{by}({f_count}/{len(tool_verifications)})"
            elif u_count > 0:
                agg = VerificationStatus.UNKNOWN
                reason = f"tools_succeeded_but_some_unknown({u_count}/{len(tool_verifications)})"
            else:
                agg = VerificationStatus.SUCCESS
                reason = f"all_tools_verified_success({s_count}/{len(tool_verifications)})"

            # Tool-level result decides. Text is NOT required after tools ran —
            # the task ends structurally once the model stops emitting tool_calls.
            if agg == VerificationStatus.FAILED:
                return AttemptDecision(
                    attempt_status=AttemptStatus.RETRY,
                    tool_verifications=tool_verifications,
                    aggregate=agg,
                    reason=f"need_more_evidence|{reason}",
                    fatal_agent_state=fatal_state,
                )

            if fatal_state:
                return AttemptDecision(
                    attempt_status=AttemptStatus.RETRY,
                    tool_verifications=tool_verifications,
                    aggregate=agg,
                    reason=f"fatal_agent_state:{attempt_state}|{reason}",
                    fatal_agent_state=fatal_state,
                )

            # Tools may all be green while the final text is a leaked tool call
            # ("default_api:read_file{...}"). The user got no answer, so retry
            # and let the model produce prose instead.
            # A plan that never closed, with no state-changing tool run after
            # the last update, means the model planned and then narrated. That
            # is exactly the turn that reported success with zero files on disk.
            plan = _plan_progress(tool_calls_log)
            if plan is not None:
                done, total, work_after = plan
                if done < total and not work_after:
                    return AttemptDecision(
                        attempt_status=AttemptStatus.RETRY,
                        tool_verifications=tool_verifications,
                        aggregate=VerificationStatus.FAILED,
                        reason=f"plan_incomplete({done}/{total})_and_no_state_change",
                        fatal_agent_state=fatal_state,
                    )

            if pseudo:
                return AttemptDecision(
                    attempt_status=AttemptStatus.RETRY,
                    tool_verifications=tool_verifications,
                    aggregate=agg,
                    reason=f"pseudo_tool_attempt_in_output|{reason}",
                    fatal_agent_state=fatal_state,
                )

            # All tools verified (success or unknown) => COMPLETED, text optional.
            return AttemptDecision(
                attempt_status=AttemptStatus.COMPLETED,
                tool_verifications=tool_verifications,
                aggregate=agg,
                reason=f"tools_verified|{reason}",
                fatal_agent_state=fatal_state,
            )

        # ---------- Text-only path (no native tool calls) ----------

        if explicit_status == "cannot_complete":
            return AttemptDecision(
                attempt_status=AttemptStatus.RETRY,
                tool_verifications=[],
                aggregate=VerificationStatus.FAILED,
                reason="model_reported_cannot_complete",
                fatal_agent_state=fatal_state,
            )

        if pseudo:
            return AttemptDecision(
                attempt_status=AttemptStatus.RETRY,
                tool_verifications=[],
                aggregate=VerificationStatus.FAILED,
                reason="model_wrote_tool_call_as_text_instead_of_native_tool_call",
                fatal_agent_state=fatal_state,
            )

        # Invariant I3/I4: empty user-visible reply is RETRY even with completed.
        if stripped is None:
            return AttemptDecision(
                attempt_status=AttemptStatus.RETRY,
                tool_verifications=[],
                aggregate=VerificationStatus.FAILED,
                reason="empty_user_reply_no_tools",
                fatal_agent_state=fatal_state,
            )

        if fatal_state:
            return AttemptDecision(
                attempt_status=AttemptStatus.RETRY,
                tool_verifications=[],
                aggregate=VerificationStatus.FAILED,
                reason=f"fatal_agent_state:{attempt_state}|no_user_reply",
                fatal_agent_state=True,
            )

        # Narration without tools: the model says "I'll create X" but never
        # called a tool. That's not completion — it's procrastination. Retry
        # and let the model actually call tools.
        if _looks_like_narration(stripped):
            return AttemptDecision(
                attempt_status=AttemptStatus.RETRY,
                tool_verifications=[],
                aggregate=VerificationStatus.FAILED,
                reason="text_only_narration_no_tools",
                fatal_agent_state=fatal_state,
            )

        return AttemptDecision(
            attempt_status=AttemptStatus.COMPLETED,
            tool_verifications=[],
            aggregate=VerificationStatus.SUCCESS,
            reason="text_only_user_reply_present",
        )

