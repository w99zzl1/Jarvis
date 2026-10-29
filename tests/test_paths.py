#!/usr/bin/env python
"""Checks for shared/paths.py.

Every writable runtime file must resolve under the per-user data directory
(``%APPDATA%\\com.jarvis.assistant`` by default) and never next to the source
tree: a packaged app lives in a read-only directory.

The agent workspace is a separate thing and must default to
``Documents\\Jarvis``, never to the installation directory.

Run:
    python tests/test_paths.py

No pytest, no network. Everything is written to temporary directories.
"""

import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import paths  # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    line = ("PASS  " if ok else "FAIL  ") + name
    if detail and not ok:
        line += f"  -> {detail}"
    print(line)
    if not ok:
        FAILED.append(name)


@contextmanager
def env(**values):
    """Set env vars for one block; ``None`` means 'delete'."""
    saved = {k: os.environ.get(k) for k in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def test_data_dir_override(tmp: Path):
    data = tmp / "data-override"
    with env(JARVIS_DATA_DIR=data, JARVIS_WORKDIR=None):
        check("app_dir honours JARVIS_DATA_DIR", paths.app_dir() == data, str(paths.app_dir()))
        check("app_dir creates the directory", data.is_dir())

        subdirs = {
            "conversations_dir": paths.conversations_dir(),
            "traces_dir": paths.traces_dir(),
            "uploads_dir": paths.uploads_dir(),
            "file_index_dir": paths.file_index_dir(),
            "cache_dir": paths.cache_dir(),
        }
        for name, path in subdirs.items():
            check(f"{name} is inside app_dir", _is_under(path, data), str(path))
            check(f"{name} creates the directory", path.is_dir(), str(path))

        files = {
            "memory_file": paths.memory_file(),
            "tasks_history_file": paths.tasks_history_file(),
            "location_cache_file": paths.location_cache_file(),
            "api_keys_file": paths.api_keys_file(),
        }
        for name, path in files.items():
            check(f"{name} is inside app_dir", path.parent == data, str(path))

        check(
            "no runtime path points into the source tree",
            not any(_is_under(p, ROOT) for p in list(subdirs.values()) + list(files.values())),
        )


def test_default_data_dir(tmp: Path):
    appdata = tmp / "appdata"
    appdata.mkdir()
    with env(JARVIS_DATA_DIR=None, APPDATA=appdata, JARVIS_WORKDIR=None):
        expected = appdata / paths.APP_DIR_NAME
        check("default app_dir is %APPDATA%\\<identifier>", paths.app_dir() == expected, str(paths.app_dir()))
        check("default app_dir is created", expected.is_dir())


def test_configured_workdir(tmp: Path):
    data = tmp / "data-config"
    with env(JARVIS_DATA_DIR=data, JARVIS_WORKDIR=None):
        check("configured_workdir is None without api_keys.json", paths.configured_workdir() is None)

        keys = paths.api_keys_file()
        keys.write_text(json.dumps({"workdir": str(tmp / "saved-ws")}), encoding="utf-8")
        check("configured_workdir reads workdir from api_keys.json", paths.configured_workdir() == (tmp / "saved-ws"))

        keys.write_text(json.dumps({"workdir": "   "}), encoding="utf-8")
        check("configured_workdir ignores a blank workdir", paths.configured_workdir() is None)

        keys.write_text(json.dumps({"workdir": 42}), encoding="utf-8")
        check("configured_workdir ignores a non-string workdir", paths.configured_workdir() is None)

        keys.write_text("{not json", encoding="utf-8")
        check("configured_workdir survives broken json", paths.configured_workdir() is None)


def test_workspace(tmp: Path):
    data = tmp / "data-ws"
    explicit = tmp / "explicit-ws"
    saved = tmp / "saved-ws"

    with env(JARVIS_DATA_DIR=data, JARVIS_WORKDIR=explicit):
        check("workspace_dir honours JARVIS_WORKDIR", paths.workspace_dir() == explicit, str(paths.workspace_dir()))
        check("workspace_dir creates JARVIS_WORKDIR", explicit.is_dir())

    with env(JARVIS_DATA_DIR=data, JARVIS_WORKDIR=None):
        paths.api_keys_file().write_text(json.dumps({"workdir": str(saved)}), encoding="utf-8")
        check("workspace_dir falls back to the saved workdir", paths.workspace_dir() == saved, str(paths.workspace_dir()))
        paths.api_keys_file().unlink()

    # Default: no env override, no saved config -> Documents\Jarvis under the
    # user profile. Run in a subprocess with a fake profile so the real
    # Documents folder is never touched.
    home = tmp / "home"
    home.mkdir()
    appdata = tmp / "appdata-ws"
    appdata.mkdir()
    child_env = {k: v for k, v in os.environ.items() if k not in ("JARVIS_DATA_DIR", "JARVIS_WORKDIR")}
    child_env["USERPROFILE"] = str(home)
    child_env["APPDATA"] = str(appdata)
    proc = subprocess.run(
        [sys.executable, "-c", "from shared import paths; print(paths.workspace_dir())"],
        cwd=str(ROOT),
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out = proc.stdout.strip().splitlines()
    got = Path(out[-1]) if proc.returncode == 0 and out else Path("<failed>")
    expected = home / "Documents" / paths.DEFAULT_WORKSPACE_NAME
    check(
        "workspace_dir defaults to Documents\\Jarvis",
        got.resolve() == expected.resolve(),
        f"{got} (stderr: {proc.stderr.strip()[:200]})",
    )


def test_contract_with_tauri():
    conf = ROOT / "src-tauri" / "tauri.conf.json"
    if not conf.is_file():
        check("tauri.conf.json present", False, str(conf))
        return
    identifier = json.loads(conf.read_text(encoding="utf-8")).get("identifier")
    check(
        "APP_DIR_NAME matches the Tauri bundle identifier",
        identifier == paths.APP_DIR_NAME,
        f"{identifier!r} != {paths.APP_DIR_NAME!r}",
    )
    main_rs = (ROOT / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")
    check(
        "Rust APP_DIR_NAME matches shared/paths.py",
        f'const APP_DIR_NAME: &str = "{paths.APP_DIR_NAME}";' in main_rs,
    )
    check(
        "Rust passes JARVIS_DATA_DIR and JARVIS_WORKDIR to the backend",
        '"JARVIS_DATA_DIR"' in main_rs and '"JARVIS_WORKDIR"' in main_rs,
    )


def main():
    print(f"repo: {ROOT}\n")
    with tempfile.TemporaryDirectory(prefix="jarvis-paths-") as raw:
        tmp = Path(raw)
        test_data_dir_override(tmp)
        print()
        test_default_data_dir(tmp)
        print()
        test_configured_workdir(tmp)
        print()
        test_workspace(tmp)
        print()
        test_contract_with_tauri()

    print()
    if FAILED:
        print(f"FAILED ({len(FAILED)}): " + ", ".join(FAILED))
        return 1
    print("All path checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
