"""Lost-file search: find files and folders BY NAME.

Two modes, deliberately different:

- `path` given  -> scoped walk *inside exactly that folder/drive*. Nothing is
  pre-filtered: if the user names a directory, they mean that directory.
- `path` empty  -> whole-system filename search served from the background
  index built by `shared.file_index`. Returns instantly because it never
  walks; if the index is still being built on a cold start it says so clearly
  instead of hanging.

This tool only answers "where is a thing by its name?". For "what's inside a
folder" or "read me a file" the `read` tool is the right call.
"""

import fnmatch
import os
import time
from typing import Dict, Any, List, Optional

from .registry import Tool, ToolResult
from shared import file_index

# Invisible/reserved folders that even a scoped walk may not enter - they need
# admin privileges and never contain a file the user lost.
_SKIP_INACCESSIBLE = {"$Recycle.Bin", "System Volume Information", "Recovery", "Windows.old"}

# The persisted index is a snapshot: a file downloaded after the last build is
# invisible to it. When the index yields nothing, look in the handful of folders
# a freshly saved file actually lands in. This is bounded (seconds), unlike a
# whole-system walk, and keeps the tool correct for its most common case.
_FALLBACK_SUBDIRS = ("Downloads", "Desktop", "Documents", "OneDrive")
_FALLBACK_BUDGET_SECONDS = 8.0


class SearchFilesTool(Tool):
    name = "find_file"
    description = (
        "Dedicated tool for finding files/folders BY NAME or GLOB when you know the name but not the location. "
        "This is the correct and preferred tool for filename-based lookup. Uses file index for whole-system search when path=''. "
        "For searching TEXT INSIDE files use grep/content search."
    )
    category = "system"
    cost = "low"
    destructive = False
    long_running = False
    group = "on_demand"
    catalog_hint = (
        "find files/folders BY NAME or GLOB across the disk - the purpose-built, "
        "fastest tool when you know the name but not the location (lost/missing "
        "apps, exe, recent downloads). Uses the background index, no shell scan"
    )

    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Filename or folder-name substring (case-insensitive). "
                    "Supports glob patterns (e.g. 'report*.pdf' or '*.py') "
                    "to match a name pattern."
                ),
            },
            "path": {
                "type": "string",
                "description": (
                    "Absolute folder or drive to search. Empty = whole system "
                    "via the index."
                ),
                "default": "",
            },
            "file_pattern": {
                "type": "string",
                "description": "Extension filter, e.g. '*.py' or 'py'",
                "default": "*",
            },
            "case_sensitive": {"type": "boolean", "default": False},
            "limit": {
                "type": "integer",
                "description": "Maximum number of results",
                "default": 50,
            },
            "reindex": {
                "type": "boolean",
                "description": "Rebuild the whole-system index in background",
                "default": False,
            },
        },
        "required": ["query"],
    }

    def __init__(self):
        file_index.ensure_built()

    def _match(self, full: str, q: str, ext: Optional[str], case_sensitive: bool) -> bool:
        name = os.path.basename(full)
        haystack = full if case_sensitive else full.lower()

        is_glob = any(c in q for c in ("*", "?", "["))
        if is_glob:
            pattern = q if case_sensitive else q.lower()
            if not fnmatch.fnmatch(name if case_sensitive else name.lower(), pattern):
                return False
        elif q not in haystack:
            return False

        if ext:
            low = haystack
            if not (low.endswith("." + ext) or low.endswith(ext)):
                return False
        return True

    def _search_scoped(
        self,
        scope: str,
        query: str,
        limit: int,
        file_pattern: str,
        case_sensitive: bool,
        deadline: Optional[float] = None,
    ) -> List[str]:
        q = query if case_sensitive else query.lower()
        ext = None
        if file_pattern and file_pattern not in ("*", ""):
            ext = file_pattern.lower().lstrip("*").lstrip(".")

        results: List[str] = []
        for root, dirs, files in os.walk(scope):
            if deadline is not None and time.monotonic() > deadline:
                return results
            dirs[:] = [d for d in dirs if d not in _SKIP_INACCESSIBLE]
            for name in files:
                full = os.path.join(root, name)
                if self._match(full, q, ext, case_sensitive):
                    results.append(full)
                    if len(results) >= limit:
                        return results
            for name in dirs:
                full = os.path.join(root, name)
                if self._match(full, q, ext, case_sensitive):
                    results.append(full)
                    if len(results) >= limit:
                        return results
        return results

    def _fallback_profile_search(
        self,
        query: str,
        limit: int,
        file_pattern: str,
        case_sensitive: bool,
    ) -> List[str]:
        home = os.path.expanduser("~")
        roots = [
            os.path.join(home, sub)
            for sub in _FALLBACK_SUBDIRS
            if os.path.isdir(os.path.join(home, sub))
        ]
        q = query if case_sensitive else query.lower()
        ext = None
        if file_pattern and file_pattern not in ("*", ""):
            ext = file_pattern.lower().lstrip("*").lstrip(".")
        matches: List[str] = []

        # Fast path: a freshly saved file is almost always a direct child of
        # Downloads/Desktop/Documents. One scandir per root, no recursion.
        for root in roots:
            try:
                with os.scandir(root) as it:
                    for entry in it:
                        if self._match(entry.path, q, ext, case_sensitive):
                            matches.append(entry.path)
                            if len(matches) >= limit:
                                return matches
            except OSError:
                continue
        if matches:
            return matches

        # Slow path only when the shallow pass missed: bounded recursive walk.
        deadline = time.monotonic() + _FALLBACK_BUDGET_SECONDS
        for root in roots:
            remaining = limit - len(matches)
            if remaining <= 0:
                break
            matches.extend(
                self._search_scoped(
                    root, query, remaining, file_pattern, case_sensitive,
                    deadline=deadline,
                )
            )
            if time.monotonic() > deadline:
                break
        return matches

    def execute(self, args: Dict[str, Any]) -> ToolResult:
        query = str(args.get("query", "")).strip()
        path = str(args.get("path", "") or "").strip()
        file_pattern = str(args.get("file_pattern", "*") or "*")
        case_sensitive = bool(args.get("case_sensitive", False))
        limited = max(1, min(int(args.get("limit", 50)), 500))
        reindex = bool(args.get("reindex", False))

        if not query:
            return ToolResult(status="error", error="query is required")

        if reindex and not path:
            file_index.rebuild()
            return ToolResult(
                status="success",
                data={"status": "reindex_started",
                      "message": "Whole-system index rebuild started in background."},
            )

        # Scoped search: the user named a concrete place, search exactly it.
        if path:
            scope = os.path.abspath(path)
            if not os.path.isdir(scope):
                return ToolResult(status="error", error=f"Directory not found: {scope}")
            try:
                res = self._search_scoped(
                    scope, query, limited, file_pattern, case_sensitive
                )
            except OSError as e:
                return ToolResult(status="error", error=f"Search failed: {e}")
            return self._finalize(res, query, file_pattern, "scoped search", limited)

        # Whole-system: never walk, only read the persisted index. Kick a
        # background refresh when the snapshot has aged out (never blocks); the
        # current snapshot keeps serving until the new one is swapped in.
        file_index.ensure_fresh()
        try:
            matches, _data, truncated = file_index.search(
                query,
                limit=limited,
                file_pattern=file_pattern,
                case_sensitive=case_sensitive,
            )
        except file_index.IndexNotReady:
            fallback = self._fallback_profile_search(
                query, limited, file_pattern, case_sensitive
            )
            if fallback:
                file_index.add_paths(fallback)
                return self._finalize(fallback, query, file_pattern, "profile walk", limited)
            return ToolResult(
                status="error",
                error=(
                    "The whole-system index is still being built in the "
                    "background (first run only). Tell the user it will be "
                    "ready in about a minute, or retry; you can also pass a "
                    "concrete `path` to search a known folder right now."
                ),
            )
        # A stale index cannot know about files saved after the last build, so
        # an empty result is verified against the usual download locations.
        if not matches:
            matches = self._fallback_profile_search(
                query, limited, file_pattern, case_sensitive
            )
            if matches:
                file_index.add_paths(matches)
                return self._finalize(
                    matches, query, file_pattern, "index + profile walk", limited
                )
        return self._finalize(matches, query, file_pattern, "global index", limited, truncated)

    @staticmethod
    def _finalize(
        paths: List[str],
        query: str,
        file_pattern: str,
        engine: str,
        limit: int,
        truncated: bool = False,
    ) -> ToolResult:
        shown = [{"file": p} for p in paths[:limit]]
        return ToolResult(
            status="success",
            data={
                "engine": engine,
                "query": query,
                "file_pattern": file_pattern,
                "matches_count": len(shown),
                "matches": shown,
                "truncated": len(paths) > limit or truncated,
            },
        )