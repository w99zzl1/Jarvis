from typing import Dict, Any, List
from ..base import Tool


class GetToolsTool(Tool):
    name = "get_tools"
    description = (
        "Load the JSON schema of one or more lazy tools, then call them directly. "
        "Call it with no arguments to see every lazy tool and its purpose. "
        "Lazy tools are listed in the system prompt; load one before calling it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "tool_name": {
                "type": "string",
                "description": "Name of a single tool to load.",
            },
            "tool_names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Names of several tools to load at once.",
            },
        },
    }

    def __init__(self, registry):
        self.registry = registry

    def execute(
        self,
        tool_name: str = None,
        tool_names: List[str] = None,
    ) -> Dict[str, Any]:
        names: list[str] = []
        if isinstance(tool_name, str) and tool_name.strip():
            names.append(tool_name.strip())
        if isinstance(tool_names, (list, tuple)):
            names.extend(n.strip() for n in tool_names if isinstance(n, str) and n.strip())
        names = list(dict.fromkeys(names))

        if not names:
            available = self.registry.list_available()
            return {
                "success": True,
                "schemas": {},
                "available_tools": available,
                "hint": (
                    "No tool name given. Pick the tool you need from "
                    "available_tools and call get_tools again with tool_name, "
                    "or answer the user directly if no tool is required."
                ),
            }

        schemas = {}
        for name in names:
            schema = self.registry.get_tool_schema(name)
            schemas[name] = schema or {
                "error": f"Tool '{name}' not found",
                "available_tools": [t["name"] for t in self.registry.list_available()],
            }

        return {
            "success": True,
            "schemas": schemas,
            "hint": "These schemas are active now — call the tools directly.",
        }
