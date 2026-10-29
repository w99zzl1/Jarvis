import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger("registry")


class VerificationStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    UNKNOWN = "unknown"


class AttemptStatus(str, Enum):
    COMPLETED = "completed"
    RETRY = "retry"


class ToolRiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PreflightCheck:
    """Preflight check result for a tool call."""
    def __init__(self, allowed: bool, risk_level: ToolRiskLevel = ToolRiskLevel.LOW,
                 reason: str = "", suggestions: list[str] = None):
        self.allowed = allowed
        self.risk_level = risk_level
        self.reason = reason
        self.suggestions = suggestions or []

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "risk_level": self.risk_level.value,
            "reason": self.reason,
            "suggestions": self.suggestions,
        }


def _preflight_check(name: str, args: dict) -> "PreflightCheck":
    """Preflight check for tool calls. Returns a check result."""
    # Shell-specific checks
    if name in ("run_powershell", "run_cmd"):
        interpreter = args.get("interpreter", "powershell")
        script = args.get("command", "")
        if not script:
            return PreflightCheck(allowed=True, risk_level=ToolRiskLevel.LOW)
        
        # Normalize script for pattern matching (handle aliases)
        script_lower = script.lower()
        
        # Normalize common aliases to their canonical forms for detection
        normalized = script_lower
        # gci, dir, ls -> Get-ChildItem
        normalized = re.sub(r'\b(gci|dir|ls)\b', 'get-childitem', normalized)
        # mi, cp, copy -> Copy-Item
        normalized = re.sub(r'\b(mi|cp|copy)\b', 'copy-item', normalized)
        # ri, del, rm -> Remove-Item
        normalized = re.sub(r'\b(ri|del|rm)\b', 'remove-item', normalized)
        # ni, mkdir -> New-Item
        normalized = re.sub(r'\b(ni|mkdir)\b', 'new-item', normalized)
        # gi, gu -> Get-Item
        normalized = re.sub(r'\b(gi|gu)\b', 'get-item', normalized)
        # % -> ForEach-Object
        normalized = re.sub(r'%', 'foreach-object', normalized)
        # ? -> Where-Object
        normalized = re.sub(r'\?', 'where-object', normalized)
        
        # Dangerous patterns: recursive scan on system drives
        if re.search(r'get-childitem\s+.*-recurse', normalized, re.IGNORECASE):
            if re.search(r'(?:^|\s)(?:c:|d:|e:|\\|/)\s*', normalized):
                return PreflightCheck(
                    allowed=False,
                    risk_level=ToolRiskLevel.CRITICAL,
                    reason="Recursive filesystem scan on system drive detected. This can take minutes and freeze the system.",
                    suggestions=[
                        "Use Get-PSDrive to check disk usage first",
                        "Target specific directories instead of recursive root scan",
                        "Use Get-PSDrive C | Select-Object Used,Free to check space"
                    ]
                )
        
        # Dangerous: recursive operations without limits
        if re.search(r'-recurse', normalized, re.IGNORECASE):
            if not re.search(r'-maxdepth|-depth|select-object\s+-first\s+\d+', normalized, re.IGNORECASE):
                return PreflightCheck(
                    allowed=True,  # Allow but warn
                    risk_level=ToolRiskLevel.HIGH,
                    reason="Recursive operation without depth limit or result limit. May traverse large directory trees.",
                    suggestions=[
                        "Add -MaxDepth parameter to limit recursion depth",
                        "Add | Select-Object -First N to limit results",
                        "Target specific subdirectories instead of root"
                    ]
                )
        
        # Dangerous: system modifications with -Force
        if re.search(r'(?:remove-item|remove-itemproperty|set-itemproperty|new-itemproperty)\s.*-force', normalized, re.IGNORECASE):
            return PreflightCheck(
                allowed=True,
                risk_level=ToolRiskLevel.HIGH,
                reason="Forced system modification detected. May cause system instability.",
                suggestions=["Remove -Force flag unless absolutely necessary", "Test without -Force first"]
            )
        
        # Expensive: WMI/CIM queries
        if re.search(r'get-ciminstance|get-wmiobject', normalized, re.IGNORECASE):
            if not re.search(r'-filter|where-object\s*\{', normalized, re.IGNORECASE):
                return PreflightCheck(
                    allowed=True,
                    risk_level=ToolRiskLevel.MEDIUM,
                    reason="WMI query without filter may query large datasets.",
                    suggestions=["Add -Filter or Where-Object clause to limit results"]
                )
    
    # Default: allow with low risk
    return PreflightCheck(allowed=True, risk_level=ToolRiskLevel.LOW)


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
        if self.error or self.status != "success":
            lines = [
                "[TOOL FAILURE]",
                f"Status: {self.status}",
            ]
            if self.error:
                lines.append(f"Error: {self.error}")
            if self.metadata.get("exit_code") is not None:
                lines.append(f"Exit code: {self.metadata['exit_code']}")
            if self.data:
                lines.append(f"Data: {json.dumps(self.data, ensure_ascii=False)[:500]}")
            return "\n".join(lines)

        if self.text is not None:
            return self.text

        if not self.data:
            return "OK"
        return json.dumps(self.data, ensure_ascii=False)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "data": self.data,
            "error": self.error,
            "metadata": self.metadata,
        }

    def __str__(self):
        status = self.status.upper()
        brief = json.dumps(self.data, ensure_ascii=False)[:200] if self.data else "(empty)"
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

    def verify(self, args: dict, result: ToolResult) -> "VerificationStatus":
        return VerificationStatus.UNKNOWN

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
                "Short English first-person phrase about what you do right now, "
                "e.g. 'reading notes notes.txt'. Shown live in chat. Max 60 chars."
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
            "Load the JSON schema of a lazy tool, then call it directly. "
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

    def execute(self, name: str, args: dict) -> ToolResult:
        # Run preflight check — log warnings but never block.
        # The Execution Planner in agent.py decides whether to proceed.
        preflight = _preflight_check(name, args)
        if preflight.risk_level in (ToolRiskLevel.HIGH, ToolRiskLevel.CRITICAL):
            logger.warning(
                f"[TOOL:{name}] RISK={preflight.risk_level.value}: {preflight.reason}. "
                f"Suggestions: {preflight.suggestions}"
            )

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
            result = tool.execute(args)
            result.execution_time = time.time() - start
            logger.info(f"[TOOL:{name}] done in {result.execution_time:.2f}s status={result.status}")
            return result
        except Exception as e:
            elapsed = time.time() - start
            logger.error(f"[TOOL:{name}] exception after {elapsed:.2f}s: {e}")
            return ToolResult(status="error", error=str(e), metadata={"execution_time": elapsed})

    def verify(self, name: str, args: dict, result: ToolResult) -> "VerificationStatus":
        tool = self.get(name)
        return tool.verify(args, result)

    def reset_all(self):
        """Reset per-request state on all registered tools."""
        for tool in self._tools.values():
            tool.reset()