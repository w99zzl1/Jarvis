import os
from typing import Dict, Any
from ..base import Tool, resolve_path


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Reads a FILE (its contents) or lists a DIRECTORY (its immediate "
        "entries). Use to check existence, browse a folder, or read code. "
        "Example: read_file(path='README.md') or read_file(path='src/')."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to a FILE or DIRECTORY. Can be absolute (e.g. C:\\\\Users\\\\...\\\\README.md) or relative to the project root (e.g. README.md or src/). A directory returns its entries.",
            },
            "offset": {
                "type": "integer",
                "description": "Line number to start reading from (0-based)",
                "default": 0
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of lines to read",
                "default": 100
            }
        },
        "required": ["path"]
    }

    def __init__(self, project_path: str):
        self.project_path = project_path

    def execute(self, path: str, offset: int = 0, limit: int = 100) -> Dict[str, Any]:
        full_path = resolve_path(self.project_path, path, sandbox=True)

        if not os.path.exists(full_path):
            return {"success": False, "error": f"File not found: {full_path}"}

        if os.path.isdir(full_path):
            try:
                entries = sorted(os.listdir(full_path))
            except Exception as e:
                return {"success": False, "error": f"Failed to list directory: {e}"}
            return {
                "success": True,
                "path": full_path,
                "is_directory": True,
                "entries": entries,
                "entries_count": len(entries),
            }

        if offset < 0:
            offset = 0
        if limit < 1:
            limit = 100

        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                total_lines = len(lines)
                selected = lines[offset:offset + limit]

                return {
                    "success": True,
                    "path": full_path,
                    "content": "".join(selected),
                    "total_lines": total_lines,
                    "offset": offset,
                    "has_more": offset + limit < total_lines
                }
        except Exception as e:
            return {"success": False, "error": str(e)}
