"""One process runner for every shell tool in Jarvis.

The two agents used to ship two different PowerShell implementations. They
disagreed on exit codes (one silently reported failed commands as success),
environment (only one set UTF-8 for child Python), stdin, and error shape.
That forced the agent to learn per-command workarounds (``python3``, ``chcp``,
``head``/``tail``, ``< NUL``) — symptoms of a broken tool contract, not of the
model.

This module defines the contract once:

* a command runs in an explicit dialect (``auto``/``powershell``/``cmd``/``sh``);
* stdin is always /dev/null — tools are non-interactive by design;
* the child environment is normalized to UTF-8 on every platform;
* the real OS exit code is returned, never guessed from output text;
* stdout and stderr are returned in full, as structured fields.

No keyword scanning, no per-task hints. If a command fails, the model sees the
actual output and the actual exit code and can reason about it itself.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from shared.cancellation import AgentCancelled


# UTF-8 for child interpreters. Without this, Python writing to a pipe uses the
# system locale (CP1251/CP866 on Russian Windows) and emits U+FFFD. Setting it
# centrally fixes Node, git, npm and any other tool that honours these vars,
# instead of patching one command at a time.
_UTF8_ENV = {
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
}


@dataclass
class ShellResult:
    stdout: str
    stderr: str
    exit_code: int
    shell: str
    duration: float = 0.0
    timed_out: bool = False
    cancelled: bool = False

    def to_dict(self) -> dict:
        return {
            "success": self.exit_code == 0 and not self.timed_out and not self.cancelled,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "shell": self.shell,
            "duration": round(self.duration, 3),
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
        }


def _child_env(extra: dict | None = None) -> dict:
    env = os.environ.copy()
    env.update(_UTF8_ENV)
    if extra:
        env.update(extra)
    return env


def _plaque_free(value: str) -> str:
    """Remove PowerShell 5.1 CLIXML wrappers from an output stream.

    CLIXML is a serialization artifact of redirecting PowerShell's error
    stream, not user output. This is format cleanup (like stripping ANSI), not
    a heuristic about what the command did.
    """
    if not value:
        return ""
    value = re.sub(r"<Objs\b.*?</Objs>", "", value, flags=re.DOTALL)
    value = value.replace("#< CLIXML", "")
    return "\n".join(line.rstrip() for line in value.splitlines()).strip()


_PS_FALLBACK = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.EXE"
_RC_MARK = "__JARVIS_RC__="

# A single wrapper for PowerShell 5.1 and 7. The RC sentinel is emitted as one
# token in parentheses: `Write-Output 'X' + 3` prints three lines (X, +, 3) in
# PowerShell's argument mode, which used to lose the exit code and made failed
# commands look successful. ErrorActionPreference stays Continue so native
# stderr (warnings) is not misread as a failure; $LASTEXITCODE carries the real
# status of the program the model ran.
_PS_WRAPPER = """$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$Error.Clear()
$__jarvis_rc = 0
try {
    & __JARVIS_USER_SCRIPT__ 2>&1 | ForEach-Object { Write-Output ($_ | Out-String -Width 4096) }
    if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) { $__jarvis_rc = $LASTEXITCODE }
    # A native program writing to stderr becomes a NativeCommandError record;
    # that is output, not a failure (a warning must not fail the command). A
    # cmdlet that really failed (Get-Item on a missing path) records its own
    # error id and must surface as a non-zero exit code.
    $__jarvis_real = $Error | Where-Object { $_.FullyQualifiedErrorId -ne 'NativeCommandError' }
    if ($__jarvis_real) { $__jarvis_rc = 1 }
} catch {
    Write-Output ($_ | Out-String -Width 4096)
    $__jarvis_rc = 1
}
Write-Output ('__JARVIS_RC_MARK__' + $__jarvis_rc)
"""


def _parse_ps_output(stdout: str) -> tuple[str, int]:
    lines = stdout.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith(_RC_MARK):
            try:
                return "\n".join(lines[:i]), int(lines[i][len(_RC_MARK):].strip())
            except ValueError:
                pass
    return stdout, 0


def strip_shell_markers(text: str) -> str:
    """Remove wrapper-only lines before shell output is shown to a user.

    Synchronous runs parse the RC sentinel in `_parse_ps_output`; background
    processes are monitored by TaskManager and need the same normalization
    without re-implementing the shell protocol there.
    """
    lines = [line for line in text.splitlines() if not line.startswith(_RC_MARK)]
    return _plaque_free("\n".join(lines))


def _resolve_shell(dialect: str) -> tuple[str, list[str]]:
    """Map a dialect name to an executable and its prefix arguments."""
    is_windows = platform.system() == "Windows"
    if dialect == "auto":
        dialect = "powershell" if is_windows else "sh"
    if dialect == "powershell":
        exe = shutil.which("pwsh") or shutil.which("powershell") or _PS_FALLBACK
        return dialect, [exe]
    if dialect == "cmd":
        return dialect, [shutil.which("cmd") or "cmd", "/c"]
    if dialect == "sh":
        return dialect, [shutil.which("sh") or "/bin/sh", "-c"]
    raise ValueError(
        f"Unknown shell dialect {dialect!r}; choose auto, powershell, cmd or sh"
    )


def _kill_tree(proc: subprocess.Popen, cid: str | None) -> None:
    """Kill the shell *and* everything it spawned.

    Killing only the shell leaves grandchildren holding the stdout pipe, so
    ``communicate()`` blocks until they finish on their own. On Windows the
    conversation's job object kills the whole tree when its handle closes.
    """
    if cid:
        try:
            from shared import agent_job

            agent_job.stop_job(cid)
            return
        except Exception:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _communicate(proc: subprocess.Popen, timeout: float, cancel,
                 cid: str | None = None) -> tuple[str, str, bool]:
    """Cooperative wait: communicate() is re-enterable after TimeoutExpired."""
    deadline = time.time() + timeout
    while True:
        try:
            out, err = proc.communicate(timeout=0.25)
            return out or "", err or "", False
        except subprocess.TimeoutExpired:
            cancelled = cancel is not None and getattr(cancel, "cancelled", False)
            timed_out = time.time() >= deadline
            if cancelled or timed_out:
                _kill_tree(proc, cid)
                out, err = proc.communicate()
                if cancelled:
                    raise AgentCancelled() from None
                return out or "", err or "", True


@dataclass
class _SpawnedShell:
    proc: subprocess.Popen
    shell: str
    tmp_dir: str | None
    job_key: str | None
    owns_job: bool


def _spawn_shell(
    command: str,
    cwd: str | None = None,
    dialect: str = "auto",
    cancel=None,
    *,
    assign_job: bool = True,
    merge_stderr: bool = False,
) -> _SpawnedShell:
    """Spawn a non-interactive shell. Shared by sync and background execution."""
    shell, prefix = _resolve_shell(dialect)
    env = _child_env()
    tmp_dir: str | None = None

    try:
        if shell == "powershell":
            tmp_dir = tempfile.mkdtemp(prefix="jarvis_sh_")
            script_path = os.path.join(tmp_dir, "user.ps1")
            wrapper_path = os.path.join(tmp_dir, "run.ps1")
            # utf-8-sig: PowerShell 5.1 detects UTF-8 in .ps1 files by BOM.
            with open(script_path, "w", encoding="utf-8-sig") as fh:
                fh.write(command)
            wrapper = _PS_WRAPPER.replace(
                "__JARVIS_USER_SCRIPT__", script_path.replace("'", "''")
            ).replace("__JARVIS_RC_MARK__", _RC_MARK)
            with open(wrapper_path, "w", encoding="utf-8-sig") as fh:
                fh.write(wrapper)
            argv = prefix + [
                "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", wrapper_path,
            ]
        elif shell == "cmd":
            # Code page is part of the dialect contract on Windows.
            env["PYTHONIOENCODING"] = "utf-8"
            argv = prefix + ["chcp 65001 >nul && " + command]
        else:
            argv = prefix + [command]

        proc = subprocess.Popen(
            argv,
            cwd=cwd or _default_cwd(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        # A turn's job is keyed by its conversation id (Stop closes it);
        # standalone calls get a private key. Background tasks deliberately
        # opt out: they are meant to outlive the turn and are owned by
        # TaskManager, whose own cancel path stops them.
        cid = getattr(cancel, "conversation_id", None) if cancel is not None else None
        job_key = cid or f"shell:{uuid.uuid4().hex}"
        owns_job = not cid
        if assign_job and os.name == "nt":
            try:
                from shared import agent_job

                agent_job.assign_proc(job_key, proc)
            except Exception:
                pass
        return _SpawnedShell(
            proc=proc,
            shell=shell,
            tmp_dir=tmp_dir,
            job_key=job_key if assign_job else None,
            owns_job=owns_job if assign_job else False,
        )
    except Exception:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def start_background_shell(
    command: str,
    cwd: str | None = None,
    dialect: str = "auto",
    cancel=None,
):
    """Spawn a shell and hand the live process to TaskManager.

    The caller gets a ``Task`` whose id the agent reports through `task_status`.
    This is an explicit tool option (not a guess from command keywords), so a
    build/install/download can run for as long as it needs instead of being
    killed at the shell tool's timeout.
    """
    spawned = _spawn_shell(
        command, cwd=cwd, dialect=dialect, cancel=cancel,
        assign_job=False, merge_stderr=True,
    )
    from shared.tasks import TaskManager, TaskType

    tmp_dir = spawned.tmp_dir
    cleanup = (lambda: shutil.rmtree(tmp_dir, ignore_errors=True)) if tmp_dir else None
    return TaskManager.get().run_in_background(
        spawned.proc, command, TaskType.CUSTOM,
        cleanup=cleanup, output_parser=strip_shell_markers,
    )


def run_shell(
    command: str,
    cwd: str | None = None,
    timeout: float = 60,
    cancel=None,
    dialect: str = "auto",
    on_chunk: Callable[[str], None] | None = None,
) -> ShellResult:
    """Execute `command` non-interactively and return its complete result.

    `dialect` selects the shell explicitly. ``auto`` uses PowerShell on Windows
    and sh elsewhere. Models that need cmd.exe syntax can ask for ``cmd``
    instead of translating it by hand.
    """
    start = time.time()
    spawned = _spawn_shell(command, cwd=cwd, dialect=dialect, cancel=cancel)
    try:
        stdout, stderr, timed_out = _communicate(
            spawned.proc, timeout, cancel, cid=spawned.job_key
        )
        exit_code = spawned.proc.returncode
    except AgentCancelled:
        return ShellResult(
            stdout="", stderr="", exit_code=1, shell=spawned.shell,
            duration=time.time() - start, cancelled=True,
        )
    finally:
        if spawned.tmp_dir:
            shutil.rmtree(spawned.tmp_dir, ignore_errors=True)
        if spawned.owns_job and os.name == "nt":
            try:
                from shared import agent_job

                agent_job.cleanup_job(spawned.job_key)
            except Exception:
                pass

    if spawned.shell == "powershell":
        stdout, exit_code = _parse_ps_output(stdout)
    stdout = _plaque_free(stdout)
    stderr = _plaque_free(stderr)
    if on_chunk and stdout:
        on_chunk(stdout)
    return ShellResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        shell=spawned.shell,
        duration=time.time() - start,
        timed_out=timed_out,
    )


def _default_cwd() -> str:
    from shared import paths

    workspace = paths.workspace_dir()
    if workspace and os.path.isdir(workspace):
        return workspace
    return os.path.expanduser("~")
