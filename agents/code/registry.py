from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

from agents.main.tools.registry import ToolResult
from agents.main.tools.search_files import SearchFilesTool

from .tools.active import (
    ReadFileTool,
    WriteFileTool,
    UpdatePlanTool,
    GetToolsTool,
    EditFileTool,
    ShellTool,
)
from .tools.lazy import WebSearchTool, SearchContentTool, GitStatusTool, GitCommitTool
from .tools.active import DiskTreeTool
from .plan_store import PlanStore


def _invoke(fn, args: dict):
    """Call a tool with `args`, adapting to the signature it actually declares.

    A blind `except TypeError` fallback used to pass the whole args dict as the
    first positional parameter, which silently produced empty results.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(**args)

    params = sig.parameters
    if any(p.kind is p.VAR_KEYWORD for p in params.values()):
        return fn(**args)
    if any(p.kind is p.VAR_POSITIONAL for p in params.values()):
        return fn(**args)

    # Conventional positional tools declare the payload as `args`. They may
    # also declare extra framework params (`on_progress`), so detect the
    # payload name instead of requiring exactly one parameter.
    first_name = next(iter(params))
    first = params[first_name]
    if (
        first_name in ("args", "_args", "kwargs")
        and first.kind in (first.POSITIONAL_ONLY, first.POSITIONAL_OR_KEYWORD)
    ):
        extra = {
            k: v for k, v in args.items()
            if k in params and k != first_name
        }
        return fn(args, **extra)

    accepted = {k: v for k, v in args.items() if k in params}
    missing = [
        name for name, p in params.items()
        if p.default is p.empty
        and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        and name not in accepted
    ]
    if missing:
        raise TypeError(
            f"missing required argument(s): {', '.join(missing)}; "
            f"got {sorted(args)}"
        )
    return fn(**accepted)


@dataclass
class _ToolAdapter:
    tool: Any

    @property
    def name(self) -> str:
        return self.tool.name

    @property
    def group(self) -> str:
        return getattr(self.tool, "group", "on_demand")

    def get_schema(self) -> dict:
        if hasattr(self.tool, "get_schema"):
            return self.tool.get_schema()
        return {"type": "function", "function": {"name": self.name, "description": getattr(self.tool, "description", ""), "parameters": getattr(self.tool, "parameters", {})}}

    def _accepts_progress(self) -> bool:
        """Whether `execute` declares an `on_progress` parameter."""
        try:
            return "on_progress" in inspect.signature(self.tool.execute).parameters
        except (TypeError, ValueError):
            return False

    def _accepts_cancel(self) -> bool:
        """Whether `execute` declares a `cancel` parameter."""
        try:
            return "cancel" in inspect.signature(self.tool.execute).parameters
        except (TypeError, ValueError):
            return False

    def execute(self, args: dict, on_progress=None, emit=None, call_id=None,
                cancel=None) -> ToolResult:
        call_args = dict(args)
        # Only tools that ask for it get the hook, and it is injected after the
        # model's args so a call cannot spoof the callback.
        if on_progress is not None and self._accepts_progress():
            call_args["on_progress"] = on_progress
        # Only the shell declares `cancel`; it polls the token to kill a running
        # child process the moment the user presses Stop.
        if cancel is not None and self._accepts_cancel():
            call_args["cancel"] = cancel
        try:
            result = _invoke(self.tool.execute, call_args)
        except Exception as e:
            return ToolResult(status="error", error=f"{type(e).__name__}: {e}")

        if isinstance(result, ToolResult):
            return result

        if not isinstance(result, dict):
            return ToolResult(status="success", data={"value": result})

        success = bool(result.get("success", result.get("ok", True)))
        status = "success" if success else ("failure" if result.get("error") else "error")
        data = {k: v for k, v in result.items() if k != "error" and k != "success"}
        return ToolResult(status=status, data=data, error=result.get("error"))


    def reset(self):
        reset = getattr(self.tool, "reset", None)
        if callable(reset):
            reset()


class CodeToolRegistry:
    def __init__(self, project_path: str, disabled: set[str] | list[str] | None = None,
                 plan_key: str | None = None, plan_store: PlanStore | None = None):
        self.project_path = project_path
        self.plan_key = plan_key
        # One store instance per agent, shared by update_plan and the engine.
        self.plan_store = plan_store if plan_store is not None else PlanStore()
        self._tools: dict[str, _ToolAdapter] = {}
        # Tools the user switched off. They stay registered (so the UI can list
        # them) but are hidden from the LLM schema, the on-demand catalog and
        # refused at call time, so they vanish from prompt and description.
        self.disabled: set[str] = {n for n in (disabled or []) if n}
        self._register_defaults()

    def is_disabled(self, name: str) -> bool:
        return name in self.disabled

    def list_all(self) -> list[dict]:
        """Every registered tool with metadata, regardless of enabled state."""
        return [
            {
                "name": name,
                "description": getattr(adapter.tool, "description", "") or "",
                "group": adapter.group,
            }
            for name, adapter in self._tools.items()
        ]

    def names_by_group(self, group: str) -> list[str]:
        """Enabled tool names in one group, for the generated prompt directory."""
        return [
            t["name"] for t in self.list_all()
            if t["group"] == group and t["name"] not in self.disabled
        ]

    def _register_defaults(self):
        active_tools = [
            ReadFileTool(self.project_path),
            WriteFileTool(self.project_path),
            UpdatePlanTool(self.project_path, plan_key=self.plan_key,
                           store=self.plan_store),
            GetToolsTool(self),
            EditFileTool(self.project_path),
            ShellTool(self.project_path),
            WebSearchTool(self.project_path),
            DiskTreeTool(self.project_path),
        ]
        lazy_tools = [
            SearchFilesTool(),
            SearchContentTool(self.project_path),
            GitStatusTool(self.project_path),
            GitCommitTool(self.project_path),
        ]
        for tool in active_tools:
            setattr(tool, "group", "always")
        for tool in lazy_tools:
            setattr(tool, "group", "on_demand")
        for tool in active_tools + lazy_tools:
            self.register(tool)

    def register(self, tool: Any):
        self._tools[tool.name] = _ToolAdapter(tool)

    def get_llm_specs(
        self,
        group_filter: str | None = None,
        include_names: set[str] | None = None,
    ) -> list[dict]:
        tools = [t for t in self._tools.values() if t.name not in self.disabled]
        if group_filter:
            tools = [t for t in tools if getattr(t, "group", "on_demand") == group_filter]
        if include_names:
            have = {t.name for t in tools}
            tools = tools + [t for t in self._tools.values()
                             if t.name in include_names and t.name not in have
                             and t.name not in self.disabled]
        return [tool.get_schema() for tool in tools]

    def get_tool_schema(self, name: str) -> dict | None:
        if name in self.disabled:
            return None
        tool = self._tools.get(name)
        if not tool:
            return None
        return tool.get_schema()

    def list_available(self, group: str | None = "on_demand") -> list[dict]:
        """Name + short purpose for every tool the model may load on demand."""
        out = []
        for name, adapter in self._tools.items():
            if name in self.disabled:
                continue
            if group and adapter.group != group:
                continue
            schema = adapter.get_schema().get("function", {})
            description = (schema.get("description") or "").strip()
            out.append({
                "name": name,
                "purpose": description[:180],
            })
        return out

    def get_prompt_directory(self) -> str:
        """Render the tool section of the system prompt.

        Lazy tools are not in the `tools=` schema, so their purpose must live
        in the prompt - otherwise the model cannot know they exist or when they
        beat a general-purpose shell command.
        """
        always = [
            t["name"] for t in self.list_all()
            if t["group"] == "always" and t["name"] not in self.disabled
        ]
        lazy = [
            t for t in self.list_all()
            if t["group"] == "on_demand" and t["name"] not in self.disabled
        ]
        lines = ["Directly available tools: " + ", ".join(always) + "."]
        if lazy:
            lines.append("")
            lines.append("Lazy tools (schema not loaded yet - load once, then call directly):")
            for tool in lazy:
                purpose = " ".join((tool["description"] or "").split())[:220]
                lines.append(f"- {tool['name']}: {purpose}")
            lines.append(
                'To use one: call get_tools(tool_name="<name>"), then call it '
                "directly. Its schema stays active for the rest of the turn. "
                "Never guess a name - call get_tools() with no arguments to list them."
            )
        return "\n".join(lines)

    def get(self, name: str) -> _ToolAdapter:
        if name not in self._tools:
            raise KeyError(name)
        return self._tools[name]

    def execute(self, name: str, args: dict, on_progress=None, emit=None, call_id=None,
                cancel=None) -> ToolResult:
        if name in self.disabled:
            return ToolResult(
                status="error",
                error=f"Tool '{name}' is disabled by the user.",
            )
        tool = self._tools.get(name)
        if not tool:
            return ToolResult(
                status="error",
                error=(
                    f"Unknown tool '{name}'. Available: "
                    f"{', '.join(sorted(t for t in self._tools if t not in self.disabled))}"
                ),
            )
        return tool.execute(args, on_progress=on_progress, cancel=cancel)

    def reset_all(self):
        for tool in self._tools.values():
            tool.reset()
