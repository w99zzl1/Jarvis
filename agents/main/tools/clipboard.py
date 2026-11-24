import subprocess
import time

from .registry import Tool, ToolResult


class ClipboardTool(Tool):
    name = "clip"
    description = "Copy text to clipboard."
    category = "system"
    cost = "low"
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
        },
        "required": ["text"],
    }

    def execute(self, args: dict) -> ToolResult:
        text = args.get("text", "")
        start = time.time()
        try:
            subprocess.run(["clip"], input=text.strip(), text=True, timeout=10)
            elapsed = time.time() - start
            return ToolResult(
                status="success",
                data={"chars_copied": len(text)},
                metadata={"execution_time": elapsed},
            )
        except Exception as e:
            elapsed = time.time() - start
            return ToolResult(status="error", error=str(e), metadata={"execution_time": elapsed})
