import os
import platform
import re
import shutil
import subprocess
import tempfile
from typing import Dict, Any

from ..base import Tool, resolve_path

_PS_FALLBACK = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.EXE"


def _default_shell() -> tuple[str, str]:
    """Pick the shell dialect the model is most likely writing for."""
    if platform.system() == "Windows":
        exe = shutil.which("powershell") or shutil.which("pwsh") or _PS_FALLBACK
        return "powershell", exe
    return "sh", shutil.which("sh") or "/bin/sh"


class ShellTool(Tool):
    name = "shell"
    description = (
        "Run a command in the project context. On Windows this executes in "
        "PowerShell 5.1 and nothing is translated: cmd.exe or unix syntax "
        "will fail. Write PowerShell only — Get-Date, Get-ChildItem, "
        "$env:USERPROFILE, Select-String, and parameters like -Recurse/-Filter. "
        "For cmd.exe syntax (dir /s /b, type, copy /b) run it through cmd.exe "
        "yourself, e.g. cmd /c \"dir /s /b *.py\". Do NOT use this to measure "
        "disk usage or explore folder layout - call disk_tree, it is one call "
        "instead of a recursive Get-ChildItem pipeline."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "path": {"type": "string", "default": "."},
            "timeout": {"type": "integer", "default": 60},
        },
        "required": ["command"],
    }

    def __init__(self, project_path: str):
        self.project_path = project_path
        self.group = "always"

    def execute(self, command: str, path: str = ".", timeout: int = 60) -> Dict[str, Any]:
        cwd = resolve_path(self.project_path, path, sandbox=True)
        shell_name, shell_exe = _default_shell()

        try:
            if shell_name == "sh":
                proc = subprocess.run(
                    [shell_exe, command],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                )
                stdout, stderr, returncode = proc.stdout, proc.stderr, proc.returncode
            else:
                stdout, stderr, returncode = _run_powershell(shell_exe, command, cwd, timeout)
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"Command timed out after {timeout}s"}
        except Exception as e:
            return {"success": False, "error": str(e)}

        stdout = _strip_clixml(stdout or "")
        stderr = _strip_clixml(stderr or "")

        # A command that exits 0 with no output is a legitimate result (an
        # empty directory, a successful mkdir, a quiet build). Report it as
        # success so the model does not treat an empty folder as a failure.
        # Only a non-zero exit or a PowerShell error marker is a failure.
        if not stdout and not stderr and returncode == 0:
            return {
                "success": True,
                "returncode": 0,
                "stdout": "",
                "stderr": "",
                "note": (
                    "Command completed successfully with no output (the "
                    "target may be empty)."
                ),
            }

        return {
            "success": returncode == 0,
            "returncode": returncode,
            "stdout": stdout[:5000],
            "stderr": stderr[:5000],
            "shell": shell_name,
        }


_RC_SENTINEL = "__JARVIS_RC__="

_WRAPPER = """$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$__rc = 0
try {{
    # Out-String, not .ToString(): Select-Object and most cmdlets emit
    # PSCustomObject, whose ToString() is an empty string - that silently
    # dropped the data and made every `... | Select-Object ...` look like it
    # "produced no output".
    & {user_script} 2>&1 | ForEach-Object {{ Write-Output ($_ | Out-String -Width 4096) }}
    $__rc = $LASTEXITCODE
}} catch {{
    Write-Output ($_ | Out-String -Width 4096)
    $__rc = 1
}}
if ($null -eq $__rc) {{ $__rc = 0 }}
Write-Output ('{sentinel}' + $__rc)
"""


def _run_powershell(
    shell_exe: str, command: str, cwd: str, timeout: int
) -> tuple[str, str, int]:
    """Run `command` and get plain-text output back.

    The command goes into its own UTF-8 BOM .ps1 and is dot-sourced from a
    wrapper with `2>&1`.  That keeps PowerShell's error stream out of CLIXML
    and out of the console codepage, so the model gets readable text.
    """
    tmp_dir = tempfile.mkdtemp(prefix="jarvis_sh_")
    user_ps1 = os.path.join(tmp_dir, "user.ps1")
    wrapper_ps1 = os.path.join(tmp_dir, "run.ps1")
    try:
        # utf-8-sig: PowerShell 5.1 only detects UTF-8 in .ps1 files via the BOM.
        with open(user_ps1, "w", encoding="utf-8-sig") as fh:
            fh.write(command)
        with open(wrapper_ps1, "w", encoding="utf-8-sig") as fh:
            fh.write(
                _WRAPPER.format(
                    user_script=user_ps1.replace("'", "''"),
                    sentinel=_RC_SENTINEL,
                )
            )

        proc = subprocess.run(
            [
                shell_exe,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                wrapper_ps1,
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    returncode = proc.returncode

    lines = stdout.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith(_RC_SENTINEL):
            try:
                returncode = int(lines[i][len(_RC_SENTINEL):].strip())
            except ValueError:
                pass
            del lines[i:]
            break

    stdout = "\n".join(lines)
    if returncode == 0 and _looks_like_powershell_error(stdout + stderr):
        returncode = 1

    return stdout, stderr, returncode


_CLIXML_OBJ = re.compile(r"<Objs\b.*?</Objs>", re.DOTALL)

# PowerShell reports most failures on the error stream and still exits 0, so a
# missing file or a bad parameter looks like a success unless the text is read.
_PS_ERROR_MARKERS = (
    "Cannot find path",
    "is not recognized as the name of a cmdlet",
    "A positional parameter cannot be found",
    "A parameter cannot be found",
    "CommandNotFoundException",
    "UnauthorizedAccessException",
    "PathNotFound",
    "DriveNotFound",
    "ProviderNotFound",
    "MethodNotFound",
    "PropertyNotFound",
    "ObjectNotFound",
    "Access is denied",
    "The system cannot find",
    "ParserError",
    "Exception",
)


def _looks_like_powershell_error(text: str) -> bool:
    return bool(text) and any(m in text for m in _PS_ERROR_MARKERS)


def _strip_clixml(text: str) -> str:
    """PowerShell 5.1 serializes its own progress record as CLIXML — pure noise.

    The marker token and the <Objs> blob are removed independently because the
    human-readable error sits between them.
    """
    if not text:
        return ""
    cleaned = _CLIXML_OBJ.sub("", text)
    cleaned = cleaned.replace("#< CLIXML", "")
    return "\n".join(line.rstrip() for line in cleaned.splitlines()).strip()
