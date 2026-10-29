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

The agent's *workspace* (the folder its file tools and shell operate in) is a
separate concept: it is user-configurable and defaults to ``Documents/Jarvis``,
never to the installation directory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

APP_DIR_NAME = "com.jarvis.assistant"
DEFAULT_WORKSPACE_NAME = "Jarvis"


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


def workspace_dir() -> Path:
    """Folder the agent works in.

    Priority: ``JARVIS_WORKDIR`` (set by the Tauri shell) -> saved config ->
    ``Documents/Jarvis``. The install directory is never used as a default.
    """
    override = os.environ.get("JARVIS_WORKDIR", "").strip()
    if override:
        path = Path(override).expanduser()
    else:
        path = configured_workdir() or (Path.home() / "Documents" / DEFAULT_WORKSPACE_NAME)
    path.mkdir(parents=True, exist_ok=True)
    return path
