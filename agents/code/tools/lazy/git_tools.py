import subprocess
from typing import Dict, Any
from ..base import Tool, resolve_path


class GitStatusTool(Tool):
    name = "git_status"
    description = "Check git repository status."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the git repository. Can be absolute or relative to the project root (default is project root)", "default": "."},
            "porcelain": {"type": "boolean", "default": True}
        }
    }

    def __init__(self, project_path: str):
        self.project_path = project_path

    def execute(self, path: str = ".", porcelain: bool = True) -> Dict[str, Any]:
        cwd = resolve_path(self.project_path, path)
        try:
            cmd = ["git", "status", "--porcelain"] if porcelain else ["git", "status"]
            result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=30)

            return {
                "success": result.returncode == 0,
                "output": result.stdout[:3000],
                "error": result.stderr if result.returncode != 0 else None
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


class GitCommitTool(Tool):
    name = "git_commit"
    description = "Create a git commit with the specified message."
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "Commit message"},
            "add_all": {"type": "boolean", "default": True}
        },
        "required": ["message"]
    }

    def __init__(self, project_path: str):
        self.project_path = project_path

    def execute(self, message: str, add_all: bool = True, path: str = ".") -> Dict[str, Any]:
        cwd = resolve_path(self.project_path, path)
        try:
            if add_all:
                subprocess.run(["git", "add", "-A"], cwd=cwd, capture_output=True, text=True, timeout=30)

            result = subprocess.run(["git", "commit", "-m", message], cwd=cwd, capture_output=True, text=True, timeout=30)

            return {
                "success": result.returncode == 0,
                "output": result.stdout[:2000],
                "error": result.stderr if result.returncode != 0 else None
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
