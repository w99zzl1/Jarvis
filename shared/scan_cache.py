"""Session cache of the last disk scan: every absolute path, off the context.

The model is shown a filtered, budgeted listing (`format_report` /
`format_compact` in `shared.disk_scan`). That is enough to *reason* about the
disk but not enough to *act* on it: "delete that videos folder you found" needs
the exact absolute path of the node, and shipping every path to the model would
burn the context window on the listing nobody reads.

So the raw entry list is parked here instead - in memory for the session and
mirrored to `cache/last_scan.json` under the app data directory so a restart
keeps the knowledge. File tools resolve short references against it
(see `agents/code/tools/base.py`).

Design rules:

* Resolution never guesses. A reference that matches several nodes is reported
  as ambiguous, because the caller may be about to delete something.
* Only paths that still exist on disk are ever returned.
* Synthetic entries (`Kind::Hidden`, the "[скрыто]" aggregates) are indexed for
  display but can never be resolved to - they do not exist as a path.
* Every function fails soft: a broken cache must not break a tool call.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("scan_cache")

# Entry kinds, mirrored from the Rust side.
_FILE, _DIR, _COLLAPSED, _HIDDEN = 0, 1, 2, 3

# `record()` is called after every scan; the raw list is already bounded by the
# scanner's max_nodes, this is only a backstop against a runaway write.
_MAX_PERSIST_ENTRIES = 20000

_CACHE_VERSION = 2

# How a reference was matched, best first. The enum order is the ranking order.
_EXACT = "exact_path"
_BASENAME = "basename"
_SUFFIX = "path_suffix"
_PARTIAL = "name_contains"

_UNSAFE = re.compile(r"[\x00-\x1f]")


@dataclass
class Entry:
    """One node from the last scan, with its absolute path."""

    path: str
    name: str
    size: int
    kind: int
    depth: int
    synthetic: bool = False

    @property
    def is_dir(self) -> bool:
        return self.kind in (_DIR, _COLLAPSED)


@dataclass
class LastScan:
    """Everything the last scan saw, indexed for lookup."""

    root: str
    total: int
    nodes_scanned: int
    entries: list[Entry] = field(default_factory=list)
    scanned_at: float = 0.0
    # lower-cased path -> index, for exact lookups
    _by_path: dict[str, int] = field(default_factory=dict, repr=False)
    # lower-cased basename -> indices
    _by_name: dict[str, list[int]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._reindex()

    def _reindex(self) -> None:
        self._by_path = {}
        self._by_name = {}
        for idx, entry in enumerate(self.entries):
            if entry.synthetic or not entry.path:
                continue
            self._by_path.setdefault(entry.path.lower(), idx)
            self._by_name.setdefault(entry.name.lower(), []).append(idx)

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def resolvable(self) -> int:
        return len(self._by_path)

    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.scanned_at) if self.scanned_at else 0.0

    # -- lookup ---------------------------------------------------------------

    def _alive(self, path: str) -> bool:
        try:
            return os.path.exists(path)
        except OSError:
            return False

    def _finish(self, kind: str, hits: list[int], ref: str) -> "Resolution":
        """Turn matching indices into a resolution, refusing to guess."""
        alive = [i for i in hits if self._alive(self.entries[i].path)]
        if not alive:
            return Resolution(path=None, kind=kind, ref=ref,
                              reason=f"no surviving {kind} match")
        # Biggest first: if the model asks for "Videos" and there is exactly one,
        # size is irrelevant; the sort only makes the ambiguity message useful.
        alive.sort(key=lambda i: -self.entries[i].size)
        top = self.entries[alive[0]]
        if len(alive) == 1:
            return Resolution(path=top.path, kind=kind, ref=ref,
                              candidates=[top.path])
        return Resolution(path=None, kind=kind, ref=ref,
                          candidates=[self.entries[i].path for i in alive[:6]],
                          reason=f"{len(alive)} nodes match {kind}")

    def lookup(self, ref: str) -> "Resolution":
        """Resolve a short reference to a full path from the last scan."""
        raw = (ref or "").strip().strip('"').strip("'")
        if not raw:
            return Resolution(path=None, kind="", ref=ref or "", reason="empty reference")
        if _UNSAFE.search(raw):
            return Resolution(path=None, kind="", ref=raw, reason="control characters")

        cand = os.path.normpath(os.path.expanduser(raw))
        if cand in (".", ""):
            return Resolution(path=self.root, kind=_EXACT, ref=raw, candidates=[self.root])

        key = cand.lower()

        # 1. The full path, exactly as scanned.
        idx = self._by_path.get(key)
        if idx is not None:
            return self._finish(_EXACT, [idx], raw)

        # 2. A bare name, or a trailing fragment of the path.
        name = key.rsplit("\\", 1)[-1].rstrip("/")
        if os.sep in name or "/" in name:
            hits = [i for i, e in enumerate(self.entries)
                    if not e.synthetic and e.path.lower().endswith("\\" + name.lstrip("\\"))]
            if hits:
                # Longest overlap wins, so `me\Videos` beats `Videos`.
                best_len = max(len(self.entries[i].path.lower().rsplit("\\", 1)[-1]) for i in hits)
                hits = [i for i in hits
                        if len(self.entries[i].path.lower().rsplit("\\", 1)[-1]) == best_len]
                return self._finish(_SUFFIX, hits, raw)

        hits = self._by_name.get(name)
        if hits:
            return self._finish(_BASENAME, hits, raw)

        # 3. A loose fragment ("video" -> "Videos"). Only when unambiguous.
        if len(name) >= 3:
            partial = [i for idx_list in self._by_name.values() for i in idx_list
                       if name in self.entries[i].name.lower()]
            if partial:
                return self._finish(_PARTIAL, partial, raw)

        return Resolution(path=None, kind="", ref=raw, reason="not in the last scan")

    def resolve(self, ref: str) -> str | None:
        return self.lookup(ref).path

    # -- persistence ----------------------------------------------------------

    def to_json(self) -> dict:
        return {
            "version": _CACHE_VERSION,
            "root": self.root,
            "total": self.total,
            "nodes_scanned": self.nodes_scanned,
            "scanned_at": self.scanned_at,
            "entries": [
                {
                    "path": e.path,
                    "name": e.name,
                    "size": e.size,
                    "kind": e.kind,
                    "depth": e.depth,
                    "synthetic": e.synthetic,
                }
                for e in self.entries[:_MAX_PERSIST_ENTRIES]
            ],
        }

    @classmethod
    def from_json(cls, data: dict) -> "LastScan | None":
        try:
            if int(data.get("version", 0)) != _CACHE_VERSION:
                return None
            entries = [
                Entry(
                    path=str(e.get("path") or ""),
                    name=str(e.get("name") or ""),
                    size=int(e.get("size") or 0),
                    kind=int(e.get("kind") or 0),
                    depth=int(e.get("depth") or 0),
                    synthetic=bool(e.get("synthetic")),
                )
                for e in (data.get("entries") or [])
                if isinstance(e, dict)
            ]
            return cls(
                root=str(data.get("root") or ""),
                total=int(data.get("total") or 0),
                nodes_scanned=int(data.get("nodes_scanned") or 0),
                entries=entries,
                scanned_at=float(data.get("scanned_at") or 0.0),
            )
        except Exception as e:  # pragma: no cover - defensive
            log.warning("[scan_cache] unreadable cache: %s", e)
            return None


@dataclass
class Resolution:
    """Outcome of a lookup: a path, or an explanation of why there is none."""

    path: str | None
    kind: str
    ref: str
    candidates: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.path is not None

    def explain(self) -> str:
        if self.ok:
            return f"{self.ref} -> {self.path} ({self.kind})"
        extra = ""
        if self.candidates:
            extra = " candidates: " + ", ".join(self.candidates)
        return f"{self.ref}: {self.reason or 'unresolved'}{extra}"


class ScanCache:
    """Process-wide holder for the last scan. Thread-safe: scans run on threads."""

    def __init__(self, cache_file: str | None = None):
        self._lock = threading.RLock()
        self._scan: LastScan | None = None
        self._cache_file = cache_file
        self._loaded_from_disk = False

    @property
    def cache_file(self) -> str:
        if self._cache_file:
            return self._cache_file
        from shared import paths

        return str(paths.cache_dir() / "last_scan.json")

    def record(self, scan) -> LastScan:
        """Store the raw entry list of a finished `shared.disk_scan.Scan`."""
        try:
            stored = _entries_from_scan(scan)
        except Exception as e:  # pragma: no cover - defensive
            log.warning("[scan_cache] could not index the scan: %s", e)
            return self.current() or _empty()
        with self._lock:
            self._scan = stored
            self._loaded_from_disk = True
        self._persist(stored)
        return stored

    def current(self) -> LastScan | None:
        """The last scan, rehydrated from disk once if this process has none."""
        with self._lock:
            if self._scan is not None:
                return self._scan
            if self._loaded_from_disk:
                return None
            self._loaded_from_disk = True
        restored = self._restore()
        if restored is not None:
            with self._lock:
                self._scan = restored
            log.info("[scan_cache] restored %d paths for %s from %s",
                     len(restored), restored.root, self.cache_file)
        return restored

    def lookup(self, ref: str) -> Resolution:
        scan = self.current()
        if scan is None:
            return Resolution(path=None, kind="", ref=ref,
                              reason="no scan in this session yet")
        return scan.lookup(ref)

    def resolve(self, ref: str) -> str | None:
        return self.lookup(ref).path

    def clear(self) -> None:
        with self._lock:
            self._scan = None
        try:
            os.remove(self.cache_file)
        except OSError:
            pass

    def stats(self) -> dict:
        scan = self.current()
        if scan is None:
            return {"scans": 0, "entries": 0, "resolvable": 0}
        return {
            "scans": 1,
            "root": scan.root,
            "entries": len(scan),
            "resolvable": scan.resolvable,
            "total_bytes": scan.total,
            "nodes_scanned": scan.nodes_scanned,
            "age_seconds": round(scan.age_seconds(), 1),
            "cache_file": self.cache_file,
            "cache_persisted": os.path.exists(self.cache_file),
        }

    def describe(self) -> str:
        """One-line system-prompt hint, so the model knows short names work."""
        scan = self.current()
        if scan is None or not len(scan):
            return ""
        return (
            f"[Last disk scan — paths known, short names resolve] "
            f"{scan.root}: {len(scan):,} paths cached, "
            f"{scan.age_seconds() / 60:.0f} min old. "
            "Any folder or file it reported can be addressed by its short name in "
            "shell / read_file / write_file / edit_file / disk_tree."
        )

    # -- disk ----------------------------------------------------------------

    def _persist(self, scan: LastScan) -> None:
        path = self.cache_file
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(scan.to_json(), fh, ensure_ascii=False)
            os.replace(tmp, path)  # atomic: a crash cannot leave a half file
        except Exception as e:
            log.warning("[scan_cache] cache write failed: %s", e)

    def _restore(self) -> LastScan | None:
        path = self.cache_file
        try:
            if not os.path.exists(path):
                return None
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                return None
            return LastScan.from_json(data)
        except Exception as e:
            log.warning("[scan_cache] cache read failed: %s", e)
            return None


def _empty() -> LastScan:
    return LastScan(root="", total=0, nodes_scanned=0, entries=[], scanned_at=time.time())


def _entries_from_scan(scan) -> LastScan:
    """Turn a native report into indexed entries with absolute paths.

    Reuses the scanner's own parent chain, so the cached paths are exactly the
    ones the model was shown - no second walk, no divergence.
    """
    from shared.disk_scan import _absolute_paths  # local import: shared->shared

    report = scan.report
    raw = list(report.entries)
    paths = _absolute_paths(raw)
    entries: list[Entry] = []
    for idx, (entry, path) in enumerate(zip(raw, paths)):
        name = entry.name or (os.path.basename(path.rstrip("\\/")) if idx else path)
        synthetic = entry.kind == _HIDDEN
        if synthetic:
            # The aggregate has no name of its own; keep the folder it describes.
            name = os.path.basename(paths[entry.parent].rstrip("\\/")) if entry.parent >= 0 else ""
        entries.append(
            Entry(
                path=path,
                name=name,
                size=int(entry.size),
                kind=int(entry.kind),
                depth=path.count("\\") - paths[0].count("\\"),
                synthetic=synthetic,
            )
        )
    entries.sort(key=lambda e: (not e.is_dir, -e.size, e.path.lower()))
    return LastScan(
        root=paths[0] if paths else report.root,
        total=int(report.total),
        nodes_scanned=int(report.nodes_scanned),
        entries=entries,
        scanned_at=time.time(),
    )


# Module-level registry: the session's single source of truth for scanned paths.
LAST_SCAN_REGISTRY = ScanCache()
