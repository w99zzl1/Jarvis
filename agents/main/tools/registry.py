import inspect
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger("registry")


class AttemptStatus(str, Enum):
    COMPLETED = "completed"
    RETRY = "retry"


@dataclass
class ToolResult:
    status: str = "success"         # "success" | "failure" | "timeout" | "error"
    data: dict = field(default_factory=dict)
    error: str | None = None
    metadata: dict = field(default_factory=dict)
    text: str | None = None         # explicit override for to_text()
    # Short digest of `text` for later turns. `text` can be large (a full scan
    # report); history keeps this once the turn that produced it is over.
    summary: str | None = None

    @property
    def success(self) -> bool:
        return self.status == "success"

    @property
    def execution_time(self) -> float:
        return self.metadata.get("execution_time", 0.0)

    @execution_time.setter
    def execution_time(self, value: float):
        self.metadata["execution_time"] = value

    def to_text(self) -> str:
        """Render the result for the model.

        Failures carry the full stdout/stderr/exit code when the tool provides
        them. Truncating a traceback to a few hundred characters used to hide
        the actual exception and made the model guess at fixes. The single cap
        below is a context-window safety limit, not a per-tool heuristic.
        """
        cap = 8000
        if self.error or self.status != "success":
            lines = ["[TOOL FAILURE]", f"Status: {self.status}"]
            if self.error:
                lines.append(f"Error: {self.error}")
            exit_code = self.metadata.get("exit_code")
            if exit_code is None:
                exit_code = self.data.get("exit_code")
            if exit_code is not None:
                lines.append(f"Exit code: {exit_code}")
            if self.data:
                for key in ("stdout", "stderr"):
                    value = self.data.get(key)
                    if value:
                        lines.append(f"{key.upper()}:\n{value}")
                other = {
                    k: v for k, v in self.data.items()
                    if k not in ("stdout", "stderr", "exit_code")
                }
                if other:
                    lines.append(json.dumps(other, ensure_ascii=False, default=str))
            return "\n".join(lines)[:cap]

        if self.text is not None:
            return self.text[:cap]

        if not self.data:
            return "OK"
        return json.dumps(self.data, ensure_ascii=False, default=str)[:cap]

    def to_dict(self) -> dict:
        # `text`/`summary` are part of the result a tool can return. Leaving them
        # out here silently lost plain-text results between the engine and the
        # streaming UI, which only ever sees this serialized form.
        return {
            "status": self.status,
            "data": self.data,
            "error": self.error,
            "metadata": self.metadata,
            "text": self.text,
            "summary": self.summary,
        }

    def __str__(self):
        status = self.status.upper()
        brief = (
            json.dumps(self.data, ensure_ascii=False, default=str)[:200]
            if self.data else "(empty)"
        )
        return f"[{status} {self.execution_time:.2f}s] {brief}"


class ToolMeta:
    category: str = "general"
    cost: str = "medium"
    destructive: bool = False
    long_running: bool = False
    streaming: bool = False
    group: str = "on_demand"      # "always" | "on_demand"
    catalog_hint: str = ""        # one-line description for catalog

    def describe(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "cost": self.cost,
            "destructive": self.destructive,
            "long_running": self.long_running,
            "streaming": self.streaming,
        }


class Tool(ToolMeta):
    name: str = ""
    description: str = ""
    parameters: dict = {}

    def execute(self, args: dict) -> ToolResult:
        raise NotImplementedError

    def reset(self):
        """Reset per-request state. Called once at the start of each agent.run()."""
        pass

    def to_openai_spec(self) -> dict:
        # Generate minimal schema to save tokens
        params = dict(self.parameters)
        # Google provider rejects "additionalProperties" in function declarations
        params.pop("additionalProperties", None)
        props = dict(params.get("properties") or {})
        for prop in props.values():
            # Remove description and default from properties to save space
            prop.pop("description", None)
            prop.pop("default", None)
        props["narrate"] = {
            "type": "string",
            "description": (
                "Short first-person phrase in the user's language about what you do "
                "right now, e.g. 'reading notes notes.txt'. Shown live in chat. "
                "Max 60 chars."
            ),
        }
        params["properties"] = props

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": params,
            },
        }


class GetToolsTool(Tool):
    """Returns the full OpenAI schema for a registered tool by name."""
    group = "always"
    def __init__(self, registry: "ToolRegistry"):
        super().__init__()
        self.name = "get_tools"
        self.description = (
            "Load the JSON schema of a registered tool, then call it directly. "
            "Call it with no tool_name to list every lazy tool with its purpose. "
            "Lazy tools are listed in the system prompt; load one before calling it."
        )
        self.parameters = {
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "Name of the tool to load schema for.",
                }
            },
        }
        self._registry = registry
        self.category = "meta"
        self.cost = "low"

    def execute(self, args: dict) -> ToolResult:
        name = args.get("tool_name")
        if not isinstance(name, str) or not name.strip():
            # No name given: answer with the catalogue instead of an error, so
            # the model can pick a tool instead of retrying blindly.
            available = [
                {"name": tool.name,
                 "purpose": (tool.description or "").strip()[:180]}
                for tool in self._registry._tools.values()
                if getattr(tool, "group", "on_demand") == "on_demand"
            ]
            return ToolResult(
                status="success",
                data={"tool_name": None, "available_tools": available},
                text=(
                    "No tool_name given. available_tools: "
                    + json.dumps(available, ensure_ascii=False)
                    + ". Call get_tools again with tool_name, or answer directly."
                ),
            )
        name = name.strip()
        try:
            if name not in self._registry._tools:
                raise KeyError(name)
            spec = self._registry.get(name).to_openai_spec()
            return ToolResult(
                status="success",
                data={"tool_name": name, "schema": spec},
                text=f"Schema for '{name}': {json.dumps(spec, ensure_ascii=False)[:400]}...",
            )
        except KeyError:
            return ToolResult(
                status="error",
                error=f"Unknown tool: {name}. Available: {', '.join(self._registry._tools.keys())}",
            )


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        self._tools[tool.name] = tool
        logger.info(f"[REGISTRY] registered tool: {tool.name} ({tool.category}, {tool.cost}, destructive={tool.destructive})")

    def get_catalog_text(self) -> str:
        """Compact catalog: 'shell — Windows commands; web — internet search'."""
        parts = []
        for name, tool in self._tools.items():
            if tool.catalog_hint:
                parts.append(f"{name} — {tool.catalog_hint}")
        return "Tools: " + "; ".join(parts) + "."

    def get_prompt_directory(self) -> str:
        """Render the tool section of the system prompt.

        Lazy tools are not in the `tools=` schema, so their purpose has to be
        spelled out here; otherwise the model has no way to prefer a narrow,
        purpose-built tool over a general shell command it can already see.
        """
        always: list[Tool] = []
        lazy: list[Tool] = []
        for tool in self._tools.values():
            group = getattr(tool, "group", "on_demand")
            (always if group == "always" else lazy).append(tool)

        lines = ["Directly available tools: " + ", ".join(t.name for t in always) + "."]
        if lazy:
            lines.append("")
            lines.append("Lazy tools (schema not loaded yet - load once, then call directly):")
            for tool in lazy:
                purpose = (
                    getattr(tool, "catalog_hint", "")
                    or " ".join((tool.description or "").split()).split(".")[0]
                )
                lines.append(f"- {tool.name}: {purpose.strip()[:200]}")
            lines.append(
                'To use one: call get_tools(tool_name="<name>"), then call it '
                "directly. Call get_tools() with no arguments to list them all."
            )
        return "\n".join(lines)

    def get_llm_specs(
        self,
        group_filter: str | None = None,
        include_names: set[str] | None = None,
    ) -> list[dict]:
        """JSON schemas for registered tools, optionally filtered by group.

        `include_names` re-adds on-demand tools whose schema the model already
        activated through get_tools, so they stay callable afterwards.
        """
        tools = list(self._tools.values())
        if group_filter:
            tools = [t for t in tools if getattr(t, "group", "on_demand") == group_filter]
        if include_names:
            have = {getattr(t, "name", None) for t in tools}
            tools = tools + [t for t in self._tools.values()
                             if getattr(t, "name", None) in include_names
                             and getattr(t, "name", None) not in have]
        return [t.to_openai_spec() for t in tools]

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if not tool:
            raise KeyError(f"Unknown tool: {name}")
        return tool

    def execute(self, name: str, args: dict, emit=None, call_id=None, on_progress=None,
                cancel=None) -> ToolResult:
        tool = self._tools.get(name)
        if not tool:
            return ToolResult(
                status="error",
                error=(
                    f"Unknown tool '{name}'. Available: "
                    f"{', '.join(sorted(self._tools))}"
                ),
            )
        logger.info(f"[TOOL:{name}] executing args={json.dumps(args, ensure_ascii=False)[:200]}")
        start = time.time()
        try:
            # Only pass emit/call_id/on_progress to tools that declare them as
            # parameters. co_varnames includes local variables (e.g. disk_tree
            # has a local `emit`), so we must inspect the signature, not
            # co_varnames.
            extra = {}
            try:
                sig_params = inspect.signature(tool.execute).parameters
            except (TypeError, ValueError):
                sig_params = {}
            if 'emit' in sig_params:
                extra['emit'] = emit
            if 'call_id' in sig_params:
                extra['call_id'] = call_id
            if 'on_progress' in sig_params:
                extra['on_progress'] = on_progress
            # Only long-running tools (the shell) declare `cancel`; the loop
            # unwinds every other tool between calls.
            if 'cancel' in sig_params:
                extra['cancel'] = cancel
            result = tool.execute(args, **extra)
            result.execution_time = time.time() - start
            logger.info(f"[TOOL:{name}] done in {result.execution_time:.2f}s status={result.status}")
            return result
        except Exception as e:
            elapsed = time.time() - start
            logger.error(f"[TOOL:{name}] exception after {elapsed:.2f}s: {e}")
            return ToolResult(status="error", error=str(e), metadata={"execution_time": elapsed})

    def reset_all(self):
        """Reset per-request state on all registered tools."""
        for tool in self._tools.values():
            tool.reset()
