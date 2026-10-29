import os
from typing import Dict, Any
from ..base import Tool, resolve_path


class EditFileTool(Tool):
    name = "edit_file"
    description = "Replaces a specific text fragment in a file (like IDE editing). oldString must be unique, otherwise use replace_all."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to a file. Can be absolute to edit any file on the system, or relative to the project root."},
            "old_string": {"type": "string", "description": "Text to replace (must match exactly)"},
            "new_string": {"type": "string", "description": "New text"},
            "replace_all": {"type": "boolean", "default": False, "description": "Replace all occurrences of old_string"}
        },
        "required": ["path", "old_string", "new_string"]
    }

    def __init__(self, project_path: str):
        self.project_path = project_path

    def execute(self, path: str, old_string: str, new_string: str, replace_all: bool = False) -> Dict[str, Any]:
        full_path = resolve_path(self.project_path, path)

        if not os.path.exists(full_path):
            return {"success": False, "error": f"File not found: {full_path}"}

        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                content = f.read()

            count = content.count(old_string)
            if count == 0:
                return {"success": False, "error": "old_string not found in file"}

            if not replace_all and count > 1:
                return {"success": False, "error": f"old_string is not unique ({count} occurrences found). Be more specific or enable replace_all."}

            new_content = content.replace(old_string, new_string, 0 if replace_all else 1)
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(new_content)

            return {
                "success": True,
                "path": full_path,
                "replacements": content.count(old_string) if replace_all else 1,
                "bytes_written": len(new_content.encode('utf-8'))
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
