"""Whole-system filename index for the lost-file search.

Why a separate module: the index must be built *once* in the background and
then live on disk. A tool call that walks C:\\ on first use is a hang, which is
exactly what the synchronous implementation suffered from.

Lifecycle
---------
- `FileIndex.ensure_built()` is called at server startup and by the tool.
  First call spawns one daemon thread that walks all drives and writes
  `index.pkl.gz` atomically (tmp + rename).
- `FileIndex.search()` never walks. It reads the persisted index and does a
  substring scan over the lowercased paths. Any scoped search (path given)
  must NOT use this index - the scoped walker lives in the tool.

The index covers both file names and directory names, so "lost folders" are
findable exactly like "lost files".
"""

from __future__ import annotations

import fnmatch
import gzip
import json
import logging
import os
import pickle
import threading
import time
from typing import List, Tuple

log = logging.getLogger("file_index")

_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_file_index"
)
_INDEX_PATH = os.path.join(_CACHE_DIR, "index.pkl.gz")
_META_PATH = os.path.join(_CACHE_DIR, "index_meta.json")
_TMP_PATH = _INDEX_PATH + ".tmp"

# The index is a point-in-time snapshot. Files downloaded after the build (the
# most common reason to search!) would never be found, so an older index is
# rebuilt in the background while the stale copy keeps serving. Refreshed both
# at startup and lazily on the first search after the threshold.
MAX_INDEX_AGE_SECONDS = 3600
# Don't re-stat the meta file on every single query - once a minute is plenty.
_FRESH_CHECK_INTERVAL = 60.0
_last_fresh_check = 0.0

# These are never useful for a *filename* search and made the old walk expand
# into tens of seconds of build before the first answer appeared. A scoped
# search does not filter by them.
_SKIP_SYSTEM_DIRS = {
    "$Recycle.Bin",
    "System Volume Information",
    "Windows",
    "Program Files",
    "Program Files (x86)",
    "Windows.old",
    "node_modules",
    "__pycache__",
    ".git",
    "target",
    "dist",
    "build",
    ".venv",
    "venv",
    ".cache",
}

_state_lock = threading.Lock()
_building = False
_started = False

# In-process cache: decompressing a whole-system index per query is the bulk
# of the latency. Load once per process, refresh on rebuild.
_cached: "_IndexData | None" = None
_load_lock = threading.Lock()


class IndexNotReady(RuntimeError):
    """Raised when the persisted index does not exist yet."""


def index_age_seconds() -> float | None:
    """Age of the persisted index in seconds, or None if it does not exist."""
    try:
        if os.path.isfile(_META_PATH):
            with open(_META_PATH, "r", encoding="utf-8") as f:
                built_at = float(json.load(f).get("built_at") or 0)
            if built_at > 0:
                return max(0.0, time.time() - built_at)
        if os.path.isfile(_INDEX_PATH):
            return max(0.0, time.time() - os.path.getmtime(_INDEX_PATH))
    except Exception:
        pass
    return None


def is_stale(max_age_seconds: float = MAX_INDEX_AGE_SECONDS) -> bool:
    age = index_age_seconds()
    return age is None or age > max_age_seconds


def ensure_fresh() -> None:
    """Rebuild in the background if the index has outgrown its max age.

    Safe to call on every query: it is throttled, never blocks, and keeps the
    current snapshot serving until the fresh one is swapped in atomically.
    """
    global _building, _last_fresh_check, _started
    now = time.time()
    if now - _last_fresh_check < _FRESH_CHECK_INTERVAL:
        return
    with _state_lock:
        _last_fresh_check = now
        if _building or not os.path.isfile(_INDEX_PATH):
            return
        if not is_stale():
            return
        _started = True
        _building = True
    threading.Thread(target=_rebuild_worker, name="file-index-builder", daemon=True).start()
    log.info(
        "[file_index] index is stale (age %.1fh > %.1fh), rebuilding in background",
        (index_age_seconds() or 0) / 3600, MAX_INDEX_AGE_SECONDS / 3600,
    )


def add_paths(paths) -> None:
    """Merge newly discovered paths into the in-memory index.

    Used by the tool when its bounded fallback locates a file the snapshot did
    not know about yet: the next query finds it instantly through the index
    instead of walking again. Persisted on the next full rebuild.
    """
    if not paths:
        return
    try:
        data = _load()
    except IndexNotReady:
        return
    for path in paths:
        if not path:
            continue
        data.low.append(path.lower())
        data.orig.append(path)


def ensure_built() -> None:
    """Start a background build if no usable index exists on disk yet."""
    global _started, _building
    with _state_lock:
        if _started:
            return
        _started = True
        if os.path.isfile(_INDEX_PATH):
            if is_stale():
                _building = True
                log.info(
                    "[file_index] index is stale (age %.1fh), rebuilding in background",
                    (index_age_seconds() or 0) / 3600,
                )
                threading.Thread(
                    target=_rebuild_worker, name="file-index-builder", daemon=True
                ).start()
            else:
                threading.Thread(target=_warm, name="file-index-warm", daemon=True).start()
                log.info("[file_index] warming cached index in background")
            return
        if _building:
            return
        _building = True

    threading.Thread(target=_build_worker, name="file-index-builder", daemon=True).start()
    log.info("[file_index] background index build started")


def _build_worker() -> None:
    try:
        _build_and_write()
    finally:
        global _building
        with _state_lock:
            _building = False


def _rebuild_worker() -> None:
    try:
        _build_and_write()
    finally:
        global _building
        with _state_lock:
            _building = False


def _warm() -> None:
    try:
        _load()
    except Exception as e:
        log.warning(f"[file_index] warm load failed: {e}")


def rebuild() -> None:
    """Drop the persisted index and rebuild it in the background."""
    global _started, _building
    with _state_lock:
        _started = True
        if _building:
            return
        _building = True

    threading.Thread(target=_rebuild_worker, name="file-index-builder", daemon=True).start()
    log.info("[file_index] background index rebuild started")


def _all_drives() -> List[str]:
    import string as _s

    drives = []
    for d in _s.ascii_uppercase:
        p = f"{d}:\\"
        try:
            if os.path.exists(p):
                drives.append(p)
        except Exception:
            pass
    return drives


def _build_and_write() -> None:
    import time as _t

    started = _t.time()
    paths: List[str] = []

    for drive in _all_drives():
        for root, dirs, files in os.walk(drive):
            dirs[:] = [d for d in dirs if d not in _SKIP_SYSTEM_DIRS]
            for name in files:
                paths.append(os.path.join(root, name))
            for name in dirs:
                paths.append(os.path.join(root, name))

    os.makedirs(_CACHE_DIR, exist_ok=True)
    tmp = _TMP_PATH + f".{os.getpid()}"
    with gzip.open(tmp, "wb") as f:
        pickle.dump(([p.lower() for p in paths], paths), f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, _INDEX_PATH)
    try:
        meta_tmp = _META_PATH + f".{os.getpid()}"
        with open(meta_tmp, "w", encoding="utf-8") as f:
            json.dump(
                {"built_at": _t.time(), "entries": len(paths)}, f, ensure_ascii=False
            )
        os.replace(meta_tmp, _META_PATH)
    except Exception as e:
        log.warning("[file_index] failed to write index metadata: %s", e)
    _drop_cache()
    log.info(
        "[file_index] built index: %d entries in %.1fs -> %s",
        len(paths), _t.time() - started, _INDEX_PATH,
    )


class _IndexData:
    __slots__ = ("low", "orig")

    def __init__(self, low: List[str], orig: List[str]):
        self.low = low
        self.orig = orig


def _load() -> _IndexData:
    global _cached
    if _cached is not None:
        return _cached
    with _load_lock:
        if _cached is not None:
            return _cached
        if not os.path.isfile(_INDEX_PATH):
            raise IndexNotReady("index not persisted yet")
        with gzip.open(_INDEX_PATH, "rb") as f:
            low, orig = pickle.load(f)
        _cached = _IndexData(low, orig)
        return _cached


def _drop_cache() -> None:
    global _cached
    _cached = None


# Block size for the fast scan. The index is stored as flat lists; searching
# joins small slices into one haystack string so `in` runs in native C instead
# of per-entry Python loops, then splits only the matching block back.
_BLOCK = 5000


def search(
    query: str,
    limit: int = 50,
    file_pattern: str = "",
    case_sensitive: bool = False,
) -> Tuple[List[str], _IndexData, bool]:
    """Substring (or glob) search over the whole-system index.

    Returns (matches, unused_index, truncated). Raises IndexNotReady if the
    index is still being built and has not been persisted yet.
    """
    data = _load()
    q = query if case_sensitive else query.lower()
    ext = None
    if file_pattern and file_pattern not in ("*", ""):
        ext = file_pattern.lower().lstrip("*").lstrip(".")
    is_glob = any(c in q for c in ("*", "?", "["))

    n = len(data.orig)
    matches: List[str] = []
    root_matches: List[str] = []

    def passes(low_path: str, orig_path: str) -> bool:
        if is_glob:
            name = os.path.basename(orig_path if case_sensitive else low_path)
            if not fnmatch.fnmatch(name, q):
                return False
        elif q not in low_path:
            return False
        if ext and not (low_path.endswith("." + ext) or low_path.endswith(ext)):
            return False
        return True

    from_idx = 0
    while from_idx < n:
        to_idx = min(from_idx + _BLOCK, n)
        low_block = data.low[from_idx:to_idx]
        orig_block = data.orig[from_idx:to_idx]
        if not is_glob:
            haystack = "\x00".join(low_block)
            if q not in haystack:
                from_idx = to_idx
                continue
        for low, orig in zip(low_block, orig_block):
            if not passes(low, orig):
                continue
            root_matches.append(orig)
            if len(root_matches) >= limit:
                return root_matches, data, True
        from_idx = to_idx
    return root_matches, data, False