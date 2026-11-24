from .registry import Tool, ToolResult
from shared.memory.store import MemoryStore


class MemoryTool(Tool):
    name = "memory"
    description = "Memory management: save, search, delete."
    category = "memory"
    cost = "low"
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["save", "search", "delete"]},
            "key": {"type": "string"},
            "value": {"type": "string"},
            "query": {"type": "string"},
        },
        "required": ["action"],
    }

    def execute(self, args: dict) -> ToolResult:
        action = args.get("action", "")
        store = MemoryStore()

        if action == "save":
            key = args.get("key", "")
            value = args.get("value", "")
            if not key or not value:
                return ToolResult(status="error", error="save requires key and value")
            msg = store.remember(key, value)
            return ToolResult(status="success", data={"key": key, "message": msg})

        if action == "search":
            query = args.get("query", "")
            if not query:
                return ToolResult(status="error", error="search requires query")
            results = store.search(query, 5)
            return ToolResult(status="success", data={"query": query, "results": results})

        if action == "delete":
            key = args.get("key", "")
            if not key:
                return ToolResult(status="error", error="delete requires key")
            msg = store.forget(key)
            return ToolResult(status="success", data={"key": key, "message": msg})

        return ToolResult(status="error", error=f"Unknown action: {action}")
