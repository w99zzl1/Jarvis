"""Shell tool for the code agent.

Uses the same ``shared.shell_exec`` runner as the main agent, so both agents
see identical exit codes, stream contents and environment. The only code-agent
specific part is path resolution against the project directory.
"""

import logging

from ..base import Tool, resolve_path
from shared.shell_exec import run_shell

logger = logging.getLogger("code.shell")


class ShellTool(Tool):
    name = "shell"
    description = (
        "Run a command in a real shell (PowerShell on Windows, sh elsewhere) "
        "and get stdout, stderr and exit code. Use `dialect='cmd'` for cmd.exe "
        "syntax or `dialect='sh'` for POSIX syntax. Commands are non-interactive "
        "(stdin is closed): to test an interactive script, write a test file "
        "that imports it and runs its functions with fixed inputs. Use "
        "read_file/grep to inspect files instead of shelling out."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command to run."},
            "path": {
                "type": "string",
                "default": ".",
                "description": "Working directory, relative to the project path.",
            },
            "dialect": {
                "type": "string",
                "enum": ["auto", "powershell", "cmd", "sh"],
                "default": "auto",
            },
            "timeout": {"type": "integer", "default": 60},
        },
        "required": ["command"],
    }

    def __init__(self, project_path: str):
        self.project_path = project_path
        self.group = "always"

    def execute(self, command: str, path: str = ".", dialect: str = "auto",
                timeout: int = 60, cancel=None) -> dict:
        cwd = resolve_path(self.project_path, path)
        try:
            result = run_shell(
                command, cwd=cwd, timeout=int(timeout or 60),
                cancel=cancel, dialect=dialect,
            )
        except Exception as e:
            logger.exception("[SHELL] failed to run %r", command)
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
        data = result.to_dict()
        if result.cancelled:
            data["error"] = "Остановлено пользователем"
        if result.timed_out:
            data["error"] = f"Command timed out after {timeout}s"
        return data
