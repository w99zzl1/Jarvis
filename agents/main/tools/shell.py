import subprocess
import secrets
import time
import base64
import io
import logging
import os
import re
import shutil
import tempfile
import threading

from shared.tasks import TaskType
from shared import agent_job

from .registry import Tool, ToolResult, VerificationStatus

logger = logging.getLogger("shell")


def _workspace() -> str:
    """Folder agent shell commands run in (user-configurable, see shared/paths.py)."""
    from shared import paths

    return str(paths.workspace_dir())


_PS_FALLBACK = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.EXE"

_CLIXML_OBJ = re.compile(r"<Objs\b.*?</Objs>", re.DOTALL)

# Runs the user script, folds the error stream into stdout, renders every record
# as plain text (PowerShell 5.1 otherwise emits CLIXML when redirected), then
# reports the native exit code on a sentinel line the caller strips back off.
_PS_WRAPPER = """\
$ErrorActionPreference = 'Continue'
& {user_script} 2>&1 | ForEach-Object {{ Write-Output ($_ | Out-String -Width 4096) }}
$rc = $LASTEXITCODE
if ($null -eq $rc) {{ $rc = 0 }}
if (-not $? -and $rc -eq 0) {{ $rc = 1 }}
Write-Output '{sentinel}' + $rc
"""

# PowerShell reports many failures on the error stream while still exiting 0,
# so exit code alone reports a broken command as a success.
_PS_ERROR_MARKERS = (
    "CommandNotFoundException",
    "The term '",
    "is not recognized as the name of a cmdlet",
    "A positional parameter cannot be found",
    "ParserError",
    "UnauthorizedAccessException",
    "PathNotFound",
    "DriveNotFound",
    "ProviderNotFound",
    "MethodNotFound",
    "PropertyNotFound",
    "ObjectNotFound",
    "Access is denied",
    "The system cannot find",
    "The process cannot access",
    "cannot be loaded because running scripts is disabled",
    "CategoryInfo",
    "Exception",
    "RedirectionOperatorError",
    "AmbiguousMatch",
)


def _strip_clixml_noise(text: str) -> str:
    """PowerShell 5.1 serialises some records as CLIXML — pure noise to a model."""
    if not text:
        return ""
    return _CLIXML_OBJ.sub("", text)


def _looks_like_powershell_error(text: str) -> bool:
    if not text:
        return False
    return any(marker in text for marker in _PS_ERROR_MARKERS)

_ps_process = None

# TaskManager lazy import to avoid circular dependency
_task_manager = None

def _get_task_manager():
    global _task_manager
    if _task_manager is None:
        from shared.tasks import TaskManager
        _task_manager = TaskManager.get()
    return _task_manager


def _classify_task(command: str) -> TaskType:
    """Label a background command so the task manager can pick a progress parser."""
    lowered = command.lower()
    if any(word in lowered for word in ("download", "curl", "wget", "invoke-webrequest")):
        return TaskType.DOWNLOAD
    if any(word in lowered for word in ("install", "pip install", "npm install", "winget")):
        return TaskType.INSTALL
    if any(word in lowered for word in ("copy", "copy-item", "robocopy", "xcopy")):
        return TaskType.COPY
    if any(word in lowered for word in ("extract", "expand-archive", "unzip", "tar ")):
        return TaskType.EXTRACT
    if any(word in lowered for word in ("scan", "get-childitem -recurse", "dir /s", "scannow")):
        return TaskType.SCAN
    return TaskType.CUSTOM


def _execute_ps(command: str, timeout: int = 60) -> ToolResult:
    """Run `command` in PowerShell, one process per command.

    This used to feed a long-lived `powershell -NoProfile -Command -` REPL over a
    single pipe with stderr merged into stdout. That session wedged permanently
    as soon as any command wrote to the error stream: PowerShell stopped
    consuming further stdin, so every later call in the same server process
    timed out and was misrouted to a background task. A fresh process per call
    also stops one command's `cd` from leaking into every later call.
    """
    start_time = time.time()
    shell_exe = shutil.which('powershell') or shutil.which('pwsh') or _PS_FALLBACK

    tmp_dir = tempfile.mkdtemp(prefix='jarvis_sh_')
    user_ps1 = os.path.join(tmp_dir, 'user.ps1')
    wrapper_ps1 = os.path.join(tmp_dir, 'run.ps1')
    sentinel = '__JARVIS_RC__' + secrets.token_hex(8) + '__'

    try:
        with io.open(user_ps1, 'w', encoding='utf-8-sig') as fh:
            fh.write(command)
        with io.open(wrapper_ps1, 'w', encoding='utf-8-sig') as fh:
            fh.write(_PS_WRAPPER.format(
                user_script=user_ps1.replace("'", "''"),
                sentinel=sentinel,
            ))

        proc = subprocess.Popen(
            [
                shell_exe, '-NoProfile', '-NonInteractive',
                '-ExecutionPolicy', 'Bypass', '-File', wrapper_ps1,
            ],
            cwd=_workspace(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
        )
        cid = agent_job.get_current_cid()
        if cid:
            try:
                agent_job.assign_proc(cid, proc)
            except Exception:
                pass
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return ToolResult(
            status='error',
            error='PowerShell launch failed: %s' % e,
            metadata={'execution_time': time.time() - start_time},
        )

    captured: dict = {}

    def _collect():
        try:
            captured['io'] = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            return

    worker = threading.Thread(target=_collect, daemon=True)
    worker.start()
    worker.join(5)

    if 'io' not in captured:
        # Long-running: hand the live process to TaskManager and move on.
        task = _get_task_manager().run_in_background(
            proc, command, _classify_task(command),
        )
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return ToolResult(
            status='success',
            data={
                'task_id': task.id,
                'message': 'Operation is running in the background. '
                           'Use task_status to check progress.',
                'output': 'Task created: %s' % task.id[:8],
            },
            metadata={
                'execution_time': 5.0,
                'long_running': True,
                'task_id': task.id,
            },
        )

    stdout, stderr = captured['io']
    elapsed = time.time() - start_time
    # Only reclaim the scripts once the child has actually exited.
    if proc.poll() is not None:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    stdout = _strip_clixml_noise(stdout or '')
    stderr = _strip_clixml_noise(stderr or '')

    exit_code = proc.returncode or 0
    lines = stdout.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith(sentinel):
            try:
                exit_code = int(lines[i][len(sentinel):].strip())
            except ValueError:
                pass
            del lines[i:]
            break

    body = '\n'.join(lines).strip()
    failure_text = stderr.strip()
    if failure_text:
        body = (body + '\n' + failure_text).strip() if body else failure_text

    if exit_code != 0 or _looks_like_powershell_error(body):
        return ToolResult(
            status='failure',
            error=(
                '%s\n[exit_code=%s] Note: run_powershell executes PowerShell — use '
                '-Recurse/-Filter style parameters, not cmd switches like /s or /b. '
                'For cmd.exe syntax use the run_cmd tool.' % (body[:600], exit_code)
            ),
            metadata={'execution_time': elapsed, 'exit_code': exit_code or 1},
        )

    return ToolResult(
        status='success',
        data={'output': body},
        metadata={'execution_time': elapsed, 'exit_code': 0},
    )


def _execute_cmd(command: str) -> ToolResult:
    """Run `command` in cmd.exe, one process per command.

    Mirrors _execute_ps: a reader thread drains the pipes so a chatty command
    cannot deadlock on a full buffer, and anything still running after 5s is
    handed to the TaskManager.
    """
    start = time.time()
    try:
        proc = subprocess.Popen(
            ["cmd", "/c", command],
            cwd=_workspace(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        cid = agent_job.get_current_cid()
        if cid:
            try:
                agent_job.assign_proc(cid, proc)
            except Exception:
                pass
    except Exception as e:
        return ToolResult(
            status="error",
            error="cmd.exe launch failed: %s" % e,
            metadata={"execution_time": time.time() - start},
        )

    captured: dict = {}

    def _collect():
        try:
            captured["out"] = proc.communicate(timeout=5)[0]
        except subprocess.TimeoutExpired:
            return

    worker = threading.Thread(target=_collect, daemon=True)
    worker.start()
    worker.join(5)

    if "out" not in captured:
        task = _get_task_manager().run_in_background(
            proc, command, _classify_task(command),
        )
        return ToolResult(
            status="success",
            data={
                "task_id": task.id,
                "message": "Operation is running in the background. "
                           "Use task_status to check progress.",
                "output": "Task created: %s" % task.id[:8],
            },
            metadata={
                "execution_time": 5.0,
                "long_running": True,
                "task_id": task.id,
            },
        )

    stdout = _strip_clixml_noise(captured["out"] or "")
    elapsed = time.time() - start
    if proc.returncode != 0:
        prefix = (stdout.strip() + "\n") if stdout.strip() else ""
        return ToolResult(
            status="failure",
            data={"output": stdout.strip()},
            error="%s[exit_code=%s]" % (prefix, proc.returncode),
            metadata={"execution_time": elapsed, "exit_code": proc.returncode},
        )
    return ToolResult(
        status="success",
        data={"output": stdout.strip()},
        metadata={"execution_time": elapsed, "exit_code": 0},
    )


class _BaseShellTool(Tool):
    category = "system"
    cost = "medium"
    destructive = True
    group = "always"
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The command line to execute.",
            },
        },
        "required": ["command"],
    }

    def verify(self, args: dict, result: ToolResult) -> VerificationStatus:
        if result.status != "success":
            return VerificationStatus.FAILED
        exit_code = result.metadata.get("exit_code")
        if exit_code is not None and exit_code != 0:
            return VerificationStatus.FAILED
        return VerificationStatus.SUCCESS


class RunPowerShellTool(_BaseShellTool):
    """Runs the command in PowerShell. No syntax translation is performed."""

    name = "run_powershell"
    description = (
        "Run a command in PowerShell. Write PowerShell syntax: Get-Date, "
        "Get-ChildItem, $env:USERPROFILE, Select-String, and parameters like "
        "-Recurse / -Filter. For cmd.exe syntax (dir /s, type, copy /b) use "
        "run_cmd instead."
    )

    def execute(self, args: dict) -> ToolResult:
        command = args.get("command", "")
        if not command.strip():
            return ToolResult(status="error", error="No command provided")
        return _execute_ps(command)


class RunCmdTool(_BaseShellTool):
    """Runs the command in cmd.exe. No syntax translation is performed."""

    name = "run_cmd"
    description = (
        "Run a command in cmd.exe. Write cmd syntax: dir, type, copy, "
        "ren, del, findstr, with switches like /s /b /n. For PowerShell "
        "cmdlets (Get-ChildItem, Get-Content) use run_powershell instead."
    )

    def execute(self, args: dict) -> ToolResult:
        command = args.get("command", "")
        if not command.strip():
            return ToolResult(status="error", error="No command provided")
        return _execute_cmd(command)


ShellTool = RunPowerShellTool