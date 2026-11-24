"""Shell tool for the main agent.

Command execution is delegated to ``shared.shell_exec`` — the same process
runner the code agent uses. Both agents therefore share one contract: real exit
codes, full stdout/stderr, non-interactive stdin, normalized UTF-8 environment,
and an explicit shell dialect. No per-command output hints live here; a failed
command is reported with its actual output so the model can reason about it.
"""

import logging

from .registry import Tool, ToolResult
from shared.shell_exec import run_shell, start_background_shell

logger = logging.getLogger("shell")


class RunPowerShellTool(Tool):
    category = "system"
    cost = "medium"
    destructive = False
    group = "always"
    name = "run_powershell"
    description = (
        "Run a command in a real shell and return its stdout, stderr and exit "
        "code. By default PowerShell on Windows and sh elsewhere. Set "
        "`dialect` to 'cmd' for cmd.exe syntax (dir /s, type, copy /b) or 'sh' "
        "for POSIX syntax. Commands are non-interactive: stdin is closed. "
        "Use read/grep/find_file to inspect files; reserve the shell for "
        "actually running programs. Set `background=true` for installs, builds "
        "or other long-running work: it returns a task_id immediately, and "
        "task_status reports progress and the final output."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command line to execute."},
            "dialect": {
                "type": "string",
                "enum": ["auto", "powershell", "cmd", "sh"],
                "default": "auto",
                "description": "Shell dialect. auto picks PowerShell on Windows, sh elsewhere.",
            },
            "timeout": {
                "type": "integer",
                "default": 60,
                "description": "Seconds before the command is killed.",
            },
            "background": {
                "type": "boolean",
                "default": False,
                "description": "Return a task_id immediately and let the command keep running; use task_status to check it.",
            },
        },
        "required": ["command"],
    }

    def execute(self, args: dict, emit=None, call_id=None, cancel=None, **kwargs) -> ToolResult:
        command = (args or {}).get("command", "")
        if not command.strip():
            return ToolResult(status="error", error="No command provided")
        dialect = (args or {}).get("dialect", "auto")
        timeout = int((args or {}).get("timeout", 60) or 60)

        if (args or {}).get("background", False):
            try:
                task = start_background_shell(command, dialect=dialect, cancel=cancel)
            except Exception as e:
                logger.exception("[SHELL] failed to start %r in background", command)
                return ToolResult(status="error", error=f"{type(e).__name__}: {e}")
            return ToolResult(
                status="success",
                data={"task_id": task.id, "output": f"Task created: {task.id}"},
                metadata={
                    "execution_time": 0.0,
                    "long_running": True,
                    "task_id": task.id,
                },
            )

        def on_chunk(text: str):
            if emit:
                emit({
                    "type": "tool_output",
                    "tool": self.name,
                    "call_id": call_id,
                    "output": text,
                    "done": False,
                })

        try:
            result = run_shell(
                command, timeout=timeout, cancel=cancel,
                dialect=dialect, on_chunk=on_chunk,
            )
        except Exception as e:
            logger.exception("[SHELL] failed to run %r", command)
            return ToolResult(status="error", error=f"{type(e).__name__}: {e}")

        data = result.to_dict()
        if result.cancelled:
            return ToolResult(status="error", error="Остановлено пользователем")
        if result.timed_out:
            return ToolResult(
                status="error",
                error=f"Command timed out after {timeout}s",
                data={"stdout": result.stdout, "stderr": result.stderr},
            )
        if result.exit_code != 0:
            return ToolResult(status="failure", data=data)
        return ToolResult(status="success", data=data)


# Backwards-compatible aliases used by older imports and callers.
ShellTool = RunPowerShellTool
