import os
import json
from .registry import Tool, ToolResult, VerificationStatus


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

        # Resolve to absolute path
        abs_path = os.path.abspath(file_path)

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
            data={"filePath": abs_path, "charsWritten": len(content)},
        )

    def verify(self, args: dict, result: ToolResult) -> VerificationStatus:
        if result.status != "success":
            return VerificationStatus.FAILED
        exit_code = result.metadata.get("exit_code")
        if exit_code is not None and exit_code != 0:
            return VerificationStatus.FAILED
        return VerificationStatus.SUCCESS