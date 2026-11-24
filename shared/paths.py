"""Single source of truth for every writable runtime file.

The desktop app is installed into a read-only location (Program Files or a
per-user Programs folder), so nothing may be written next to the source tree.
All user data lives under one per-user directory:

    %APPDATA%\\com.jarvis.assistant\\     (default)
    $JARVIS_DATA_DIR                      (override: tests, portable builds)

Layout::

    api_keys.json        Tauri config (written by the Rust side)
    conversations/       chat history
    traces/              agent traces
    uploads/             chat attachments
    _file_index/         whole-disk filename index
    cache/               scan cache
    memory_store.json    long-term memory
    tasks_history.json   background task log
    location_cache.json  geo lookup cache

The agent's *workspace* is just a hint: the folder the user pointed at so the
agent knows where "the project" lives. It has no default and never restricts
what the file tools may touch; it only gives relative paths and the shell a
starting point.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

APP_DIR_NAME = "com.jarvis.assistant"


def app_dir() -> Path:
    """Per-user directory holding everything the app writes."""
    override = os.environ.get("JARVIS_DATA_DIR", "").strip()
    if override:
        path = Path(override).expanduser()
    else:
        appdata = os.environ.get("APPDATA")
        path = Path(appdata) / APP_DIR_NAME if appdata else Path.home() / ".jarvis"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _subdir(name: str) -> Path:
    path = app_dir() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def conversations_dir() -> Path:
    return _subdir("conversations")


def traces_dir() -> Path:
    return _subdir("traces")


def uploads_dir() -> Path:
    return _subdir("uploads")


def file_index_dir() -> Path:
    return _subdir("_file_index")


def cache_dir() -> Path:
    return _subdir("cache")


def memory_file() -> Path:
    return app_dir() / "memory_store.json"


def tasks_history_file() -> Path:
    return app_dir() / "tasks_history.json"


def location_cache_file() -> Path:
    return app_dir() / "location_cache.json"


def api_keys_file() -> Path:
    return app_dir() / "api_keys.json"


def configured_workdir() -> Path | None:
    """`workdir` saved by the settings UI, if any."""
    path = api_keys_file()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    raw = data.get("workdir") if isinstance(data, dict) else None
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser()
    return None


def workspace_dir() -> Path | None:
    """Folder the user asked the agent to treat as "the project", if any.

    There is deliberately no default: an unset workspace means the user has
    not told Jarvis where their project is, so the agent works with absolute
    paths. The workspace is a hint for relative paths / shell cwd and for the
    system prompt, never an access restriction.
    """
    override = os.environ.get("JARVIS_WORKDIR", "").strip()
    if override:
        return Path(override).expanduser()
    return configured_workdir()


def resolve_path(path: str, *, base: Path | None = None) -> Path:
    """Resolve an agent-supplied path without sandboxing it.

    Absolute paths are used exactly as given. Relative paths are resolved
    against ``base`` when provided, otherwise against the user's workspace,
    otherwise against the user's home directory. Nothing is clamped or
    rejected based on the workspace: the agent may work anywhere the task
    requires.
    """
    target = Path(path).expanduser()
    if target.is_absolute():
        return target.resolve()

    root = base or workspace_dir() or Path.home()
    return (root / target).resolve()


# =============================================================================
# Only these commands/patterns are allowed. Everything else requires explicit
# user confirmation (or is blocked entirely for Main Jarvis).
#
# Design principle: allow read-only, informational, and development commands.
# Shell commands are not filtered. The model decides what to run; there is no
# allowlist or blocklist here (that would be a Claude Code thing it doesn't
# have either).


def get_allowlist_help() -> str:
    return "No shell command restrictions."
