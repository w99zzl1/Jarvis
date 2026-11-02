import os
from typing import Dict, Any
from ..base import Tool, resolve_path, python_syntax_error


class WriteFileTool(Tool):
    name = "write_file"
    description = "Writes content to a file. Creates directories as needed."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to a file. Can be absolute to write anywhere on the system, or relative to the project root."},
            "content": {"type": "string", "description": "Content to write"},
            "append": {"type": "boolean", "default": False}
        },
        "required": ["path", "content"]
    }

    def __init__(self, project_path: str):
        self.project_path = project_path

    def execute(self, path: str, content: str, append: bool = False) -> Dict[str, Any]:
        full_path = resolve_path(self.project_path, path, sandbox=True)

        try:
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            mode = 'a' if append else 'w'

            with open(full_path, mode, encoding='utf-8') as f:
                f.write(content)

            result = {
                "success": True,
                "path": full_path,
                "bytes_written": len(content.encode('utf-8')),
                "operation": "append" if append else "write",
            }
            syntax_error = python_syntax_error(full_path)
            if syntax_error:
                result["syntax_ok"] = False
                result["warning"] = (
                    f"File was written but does not parse as Python: {syntax_error}. "
                    "Fix it before continuing."
                )
            return result
        except Exception as e:
            return {"success": False, "error": str(e)}
