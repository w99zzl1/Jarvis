"""Tool execution logic for the agent."""

import inspect
import json
import logging
import time
from typing import Any

from .tools.registry import ToolResult
from shared.cancellation import AgentCancelled
from shared.llm_client import LLMClient

logger = logging.getLogger("agent.tool_executor")


# Tool names that mutate disk/system state. After any of these runs, read-file
# caches are invalidated so the model never replays a stale file listing or
# content after an edit/write/shell.
_MUTATING_TOOLS = frozenset({
    "write_file", "write", "edit_file", "edit",
    "shell_tool", "shell", "run_powershell",
    "git_commit", "git_push", "git_add",
    "delete_file", "delete", "remove", "move", "rename", "copy",
    "append_file", "append",
})
# Read tool name prefixes whose sig_cache entries are invalidated after a
# mutating tool runs.
_READ_TOOL_PREFIXES = ("read_file:", "read:", "grep:", "disk_tree:", "find_file:")

# ONLY idempotent reads may be served from the per-attempt result cache.
# It used to cache every tool, including `run_powershell` / `shell`: a repeated
# `git status` returned the answer from several minutes ago, and repeated
# writes/clipboard/memory calls silently did nothing. A cached shell result is
# not just stale — it is a lie the model acts on. Everything outside this
# allowlist is executed every single time.
_CACHEABLE_TOOLS = frozenset({
    "read", "read_file", "grep", "find_file", "disk_tree",
})


def _is_cacheable(name: str) -> bool:
    return name in _CACHEABLE_TOOLS


class ToolExecutor:
    """Handles tool execution, caching, and progress reporting."""

    def __init__(self, registry, event_sink=None, llm_provider: str | None = None,
                 event_log=None):
        self.registry = registry
        self._event_sink = event_sink
        self.llm_provider = llm_provider
        # Shared transcript: tool output must land in the same EventLog the
        # model replays on its next iteration. Without it the model only sees
        # its own narration and can never read a finished scan.
        self.log = event_log

    def _emit(self, event: dict):
        if self._event_sink:
            try:
                self._event_sink(event)
            except Exception:
                pass

    def execute_tool(self, name: str, args: dict, call_id: str, attempt_index: int = 0,
                     cancel=None) -> ToolResult:
        """Run a tool, wiring the progress hook when the registry takes one."""
        execute = self.registry.execute
        try:
            params = inspect.signature(execute).parameters
        except (TypeError, ValueError):
            params = {}
        extra = {"emit": self._emit, "call_id": call_id}
        if "on_progress" in params:
            extra["on_progress"] = self._progress_reporter(name, call_id, attempt_index)
        # Only long-running tools (the shell) declare `cancel`; the registry
        # forwards it on to them so they can kill their child process on Stop.
        if "cancel" in params and cancel is not None:
            extra["cancel"] = cancel
        return execute(name, args, **extra)

    def _progress_reporter(self, tool_name: str, call_id: str, attempt_index: int):
        """Build the `on_progress` hook a long-running tool can call."""
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
                "attempt": attempt_index,
            })
        return report

    def execute_all(
        self,
        tool_calls,
        tc_list: list,
        history: list[str],
        attempt_index: int = 0,
        sig_cache: dict[str, ToolResult] | None = None,
        loaded_tool_names: set[str] | None = None,
        cancel=None,
    ) -> list[dict]:
        """Execute all tool calls and return execution entries."""
        entries: list[dict] = []
        calls = list(zip(tool_calls, tc_list))
        for idx, (tc, tc_info) in enumerate(calls):
            if cancel is not None and getattr(cancel, "cancelled", False):
                # The assistant message already carries every tool_call, and a
                # valid transcript needs one tool result per call. Close the
                # not-yet-run ones with a cancelled stub before unwinding.
                for pending, _ in calls[idx:]:
                    if self.log is not None:
                        self.log.add_tool_result(
                            pending.id, pending.function.name,
                            "[cancelled by user]",
                            result_status="error", duration=0.0,
                        )
                raise AgentCancelled()
            call_id = tc_info["id"]
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                logger.warning(
                    f"[TOOL_EXECUTOR] Tool '{name}' had unparseable arguments, treating as empty: "
                    f"{tc.function.arguments[:120]}"
                )
                args = {}

            narrate = args.pop("narrate", None)
            if isinstance(narrate, str) and narrate.strip():
                self._emit({"type": "agent_message", "message": narrate.strip()})

            sig = f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"
            history.append(sig)

            cacheable = _is_cacheable(name)
            cached = sig_cache.get(sig) if (sig_cache is not None and cacheable) else None
            if cached is not None:
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
                    # A replayed call must close the UI activity row exactly like
                    # a real one; without a result the row renders as empty.
                    "result": tool_result.to_dict(),
                })
                if self.log is not None:
                    self.log.add_tool_result(
                        call_id, name, self.format_result(tool_result),
                        getattr(tool_result, "status", "success"), 0.0,
                        summary=getattr(tool_result, "summary", None),
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
                tool_result: ToolResult = self.execute_tool(
                    name, args, call_id, attempt_index, cancel=cancel,
                )
            except Exception as e:
                logger.warning(f"[TOOL_EXECUTOR] Tool '{name}' raised {type(e).__name__}: {e}")
                available = ", ".join(sorted(getattr(self.registry, "_tools", {})))
                tool_result = ToolResult(
                    status="error",
                    error=f"{type(e).__name__}: {e}. Available tools: {available}",
                )
            tool_result.execution_time = time.time() - exec_start

            if sig_cache is not None and cacheable:
                sig_cache[sig] = tool_result
            # Mutating tools invalidate read caches so the model doesn't
            # replay stale file contents after an edit/write/shell.
            if name in _MUTATING_TOOLS and sig_cache is not None:
                for key in list(sig_cache.keys()):
                    if key.startswith(_READ_TOOL_PREFIXES):
                        sig_cache.pop(key, None)

            # Surface plan updates to the UI immediately.
            if name == "update_plan" and tool_result.success:
                self._emit({
                    "type": "plan_updated",
                    "plan": (tool_result.data or {}).get("steps"),
                })

            # Lazy-load tool schema from get_tools execution
            if name == "get_tools" and tool_result.success and loaded_tool_names is not None:
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
                            loaded_tool_names.add(loaded_name)
                            logger.info(f"[TOOL_EXECUTOR] Loaded full schema for tool: {loaded_name}")
                except Exception:
                    logger.debug("[TOOL_EXECUTOR] get_tools schema load failed", exc_info=True)

            self._emit({
                "type": "tool_finished",
                "tool": name,
                "status": tool_result.status,
                "duration": round(tool_result.execution_time, 3),
                "attempt": attempt_index,
                "call_id": call_id,
                "result": tool_result.to_dict(),
            })

            # Persist the result into the shared transcript so the model can
            # read it on the next iteration and the turn can be replayed later.
            if self.log is not None:
                result_text = self.format_result(tool_result)
                self.log.add_tool_result(
                    call_id, name, result_text,
                    tool_result.status, tool_result.execution_time,
                    summary=getattr(tool_result, "summary", None),
                )

            entries.append({
                "call_id": call_id,
                "tool": name,
                "args": args,
                "result": tool_result,
            })

        return entries

    def format_result(self, result: ToolResult) -> str:
        """Format tool result for the transcript (single cap lives in to_text)."""
        return result.to_text()