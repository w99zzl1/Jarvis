import os
import json
from shared.paths import resolve_path
from .registry import Tool, ToolResult


class EditTool(Tool):
    name = "edit"
    description = "Performs exact string replacements in files. Requires using Read tool at least once before editing."
    category = "system"
    cost = "medium"
    destructive = True
    group = "on_demand"
    parameters = {
        "type": "object",
        "properties": {
            "filePath": {
                "type": "string",
                "description": "The absolute path to the file to edit",
            },
            "oldString": {
                "type": "string",
                "description": "The exact string to search for and replace",
            },
            "newString": {
                "type": "string",
                "description": "The string to replace oldString with",
            },
            "replaceAll": {
                "type": "boolean",
                "description": "Replace all occurrences of oldString (default false)",
            },
        },
        "required": ["filePath", "oldString", "newString"],
        "additionalProperties": False,
    }

    def execute(self, args: dict) -> ToolResult:
        file_path = args.get("filePath", "")
        old_string = args.get("oldString", "")
        new_string = args.get("newString", "")
        replace_all = args.get("replaceAll", False)

        if not file_path or not old_string:
            return ToolResult(status="error", error="No file path or old string provided")

        # Resolve the path; the workspace is a base hint, not a restriction
        try:
            abs_path = resolve_path(file_path)
        except Exception as e:
            return ToolResult(
                status="error",
                error=f"Invalid path: {e}",
            )

        if not abs_path.exists():
            return ToolResult(
                status="error",
                error=f"Path does not exist: {abs_path}",
            )

        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as e:
            return ToolResult(
                status="error",
                error=f"Failed to read file: {e}",
            )

        if old_string not in content:
            return ToolResult(
                status="error",
                error=f"String not found in file: {old_string[:50]}...",
            )

        if replace_all:
            new_content = content.replace(old_string, new_string)
        else:
            new_content = content.replace(old_string, new_string, 1)

        try:
            with open(abs_path, "w", encoding="utf-8", errors="replace") as f:
                f.write(new_content)
        except Exception as e:
            return ToolResult(
                status="error",
                error=f"Failed to write file: {e}",
            )

        return ToolResult(
            status="success",
            data={
                "filePath": str(abs_path),
                "oldString": old_string,
                "newString": new_string,
                "replaceAll": replace_all,
                "charsAffected": len(old_string),
            },
        )
