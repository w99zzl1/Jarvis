import os
import json
from .registry import Tool, ToolResult, VerificationStatus


class ReadTool(Tool):
    name = "read"
    description = (
        "List what is inside a directory (like `dir`/`ls`) OR read the "
        "contents of a file, using a known exact path. Use this when you "
        "already know where something is. To LOCATE something by name across "
        "the whole PC, use `search_files` instead; to find TEXT inside files "
        "in the project, use a code-agent content search."
    )
    category = "system"
    cost = "low"
    destructive = False
    group = "on_demand"
    parameters = {
        "type": "object",
        "properties": {
            "filePath": {
                "type": "string",
                "description": "The absolute path to the file or directory to read",
            },
            "offset": {
                "type": "number",
                "description": "The line number to start reading from (1-indexed)",
            },
            "limit": {
                "type": "number",
                "description": "The maximum number of lines to read (defaults to 2000)",
            },
        },
        "required": ["filePath"],
    }

    def execute(self, args: dict) -> ToolResult:
        file_path = args.get("filePath", "")
        offset = args.get("offset", 1)
        limit = args.get("limit", 2000)

        if not file_path:
            return ToolResult(status="error", error="No file path provided")

        # Resolve to absolute path
        abs_path = os.path.abspath(file_path)

        if not os.path.exists(abs_path):
            return ToolResult(
                status="error",
                error=f"Path does not exist: {abs_path}",
            )

        if os.path.isdir(abs_path):
            try:
                entries = os.listdir(abs_path)
                return ToolResult(
                    status="success",
                    data={"directory": abs_path, "entries": entries},
                )
            except Exception as e:
                return ToolResult(
                    status="error",
                    error=f"Failed to read directory: {e}",
                )

        # Read file
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except Exception as e:
            return ToolResult(
                status="error",
                error=f"Failed to read file: {e}",
            )

        # Apply offset and limit (1-indexed offset)
        start = max(0, offset - 1)
        end = start + limit
        selected_lines = lines[start:end]

        # Format with line numbers
        formatted_lines = [
            f"{i + start + 1}: {line.rstrip(chr(10))}" for i, line in enumerate(selected_lines)
        ]

        return ToolResult(
            status="success",
            data={
                "filePath": abs_path,
                "content": "\n".join(formatted_lines),
                "totalLines": len(lines),
            },
        )

    def verify(self, args: dict, result: ToolResult) -> VerificationStatus:
        if result.status != "success":
            return VerificationStatus.FAILED
        exit_code = result.metadata.get("exit_code")
        if exit_code is not None and exit_code != 0:
            return VerificationStatus.FAILED
        return VerificationStatus.SUCCESS