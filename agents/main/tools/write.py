import os
import json
from shared.paths import resolve_path
from .registry import Tool, ToolResult


class WriteTool(Tool):
    name = "write"
    description = "Writes a file to the local filesystem. Will overwrite an existing file if one exists at the provided path."
    category = "system"
    cost = "medium"
    destructive = True
    group = "on_demand"
    parameters = {
        "type": "object",
        "properties": {
            "filePath": {
                "type": "string",
                "description": "The absolute path to the file to write",
            },
            "content": {
                "type": "string",
                "description": "The content to write to the file",
            },
        },
        "required": ["filePath", "content"],
        "additionalProperties": False,
    }

    def execute(self, args: dict) -> ToolResult:
        file_path = args.get("filePath", "")
        content = args.get("content", "")

        if not file_path:
            return ToolResult(status="error", error="No file path provided")

        # Resolve the path; the workspace is a base hint, not a restriction
        try:
            abs_path = resolve_path(file_path)
        except Exception as e:
            return ToolResult(
                status="error",
                error=f"Invalid path: {e}",
            )

        try:
            with open(abs_path, "w", encoding="utf-8", errors="replace") as f:
                f.write(content)
        except Exception as e:
            return ToolResult(
                status="error",
                error=f"Failed to write file: {e}",
            )

        return ToolResult(
            status="success",
            data={"filePath": str(abs_path), "charsWritten": len(content)},
        )
