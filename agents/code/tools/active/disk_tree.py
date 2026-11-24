from __future__ import annotations

import os
import threading
from collections.abc import Callable

from typing import Any, Dict, List, Optional

from shared.disk_scan import (
    ScannerMissing,
    format_compact,
    format_report,
    load_scanner,
    scan,
    scanner_location,
)
from shared.scan_cache import LAST_SCAN_REGISTRY

from ..base import Tool, resolve_path

# The native side already throttles per worker thread, but a 16-core machine
# still produces dozens of samples a second. One UI update every 100 ms is
# plenty and keeps the callback cheap while it holds the GIL.
_PROGRESS_MIN_INTERVAL = 0.1


class DiskTreeTool(Tool):
    """Disk layout of a folder, subtree sizes, from the native Rust scanner."""

    name = "disk_tree"
    category = "system"
    cost = "medium"
    destructive = False
    long_running = True
    group = "on_demand"
    catalog_hint = (
        "Folder structure and subtree sizes via native disk scanner — "
        "for questions like 'what takes space', 'where is the biggest folder', "
        "'what does drive D: look like'. Not for filename search — use find_file."
    )
    description = (
        "Show what's on disk: absolute paths with subtree sizes, from the native "
        "Rust scanner (in-process, no subprocess). Answers \"what is on disk "
        "D:\\\", \"what fills this folder\", \"where is the biggest file\". "
        "Use it for LAYOUT and SPACE; use search_files / search_content to find "
        "text inside files. Always prefer a specific folder over a whole drive: "
        "a drive holds ~1M nodes, so it costs far more and can hit the timeout. "
        "Symlinks and junctions are skipped, so linked data is never counted "
        "twice. Children below min_ratio of their folder's size are summed into "
        "one '[hided in ...]' line right under the folder's big children. Output "
        "is budgeted - if the footer says entries were hidden, re-run with a "
        "higher max_depth / max_nodes, a lower min_size_mb or min_ratio, or "
        "prune_junk=false to see everything. Hidden is not the same as absent, "
        "never conclude a folder is empty from a truncated report."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Directory to scan. Relative paths resolve against the "
                    "project root. Default: the project root itself."
                ),
                "default": ".",
            },
            "max_depth": {
                "type": "integer",
                "description": "How many levels below the root to descend. Default 4.",
                "default": 4,
            },
            "max_nodes": {
                "type": "integer",
                "description": "Hard cap on nodes in the printed report. Default 500.",
                "default": 500,
            },
            "max_children": {
                "type": "integer",
                "description": (
                    "Largest children shown per directory, sorted by subtree "
                    "size. Default 8."
                ),
                "default": 8,
            },
            "min_size_mb": {
                "type": "number",
                "description": (
                    "Hide nodes whose whole subtree is smaller than this. 0 "
                    "disables the floor. Default 0 - prefer raising max_nodes "
                    "over setting a big floor."
                ),
                "default": 0,
            },
            "min_ratio": {
                "type": "number",
                "description": (
                    "Per-folder relative floor: a child is listed only if its own "
                    "size is at least this fraction of the folder's size. Smaller "
                    "children are summed into one '[hided in ...]' line under the "
                    "folder's big children. 0 disables it. Default 0.02."
                ),
                "default": 0.02,
            },
            "prune_junk": {
                "type": "boolean",
                "description": (
                    "Prune the built-in temp/cache/system list (node_modules, "
                    "__pycache__, temp, .cache, ...). Set false to see the raw "
                    "listing. Default true."
                ),
                "default": True,
            },
            "exclude": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Extra directory NAMES to prune (with their whole subtree), "
                    "on top of the built-in list."
                ),
            },
            "include": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Directory names that must NOT be pruned even if they match "
                    "the junk list, e.g. [\"temp\"] for a project folder named temp."
                ),
            },
            "files": {
                "type": "boolean",
                "description": "Include files, not just directories. Default true.",
                "default": True,
            },
            "timeout": {
                "type": "integer",
                "description": (
                    "Seconds before the scan is aborted. Default 120, max 900."
                ),
                "default": 120,
            },
        },
        "required": [],
    }

    def __init__(self, project_path: str | None = None):
        self.project_path = project_path or os.getcwd()

    catalog_hint = (
        "Show folder structure and subtree sizes via native disk scanner — "
        "for questions like 'what takes space', 'where is the biggest folder', "
        "'what does drive D: look like'. Not for filename search — use find_file."
    )

    def reset(self):
        pass

    def to_openai_spec(self) -> dict:
        params = dict(self.parameters)
        # Google provider rejects "additionalProperties" in function declarations
        params.pop("additionalProperties", None)
        props = dict(params.get("properties") or {})
        for prop in props.values():
            prop.pop("description", None)
            prop.pop("default", None)
        props["narrate"] = {
            "type": "string",
            "description": (
                "Short first-person phrase in the user's language about what you do "
                "right now, e.g. 'scanning folder Downloads'. Shown live in chat. "
                "Max 60 chars."
            ),
        }
        params["properties"] = props
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": params,
            },
        }

    def execute(
        self,
        args: dict,
        on_progress: Callable[[dict], None] | None = None,
    ) -> "ToolResult":
        from agents.main.tools.registry import ToolResult

        path = args.get("path", ".")
        max_depth = int(args.get("max_depth", 4))
        max_nodes = int(args.get("max_nodes", 500))
        max_children = int(args.get("max_children", 8))
        min_size_mb = float(args.get("min_size_mb", 0.0))
        min_ratio = float(args.get("min_ratio", 0.02))
        prune_junk = bool(args.get("prune_junk", True))
        exclude = args.get("exclude")
        include = args.get("include")
        files = bool(args.get("files", True))
        timeout = int(args.get("timeout", 120))

        try:
            load_scanner()
        except ScannerMissing as e:
            return ToolResult(status="error", error=str(e))

        target = resolve_path(self.project_path, path)

        # The native walk calls this from its own worker threads while the GIL is
        # free, so it has to stay short and must never raise into the extension.
        emit = _throttled(on_progress)

        try:
            result = scan(
                target,
                max_depth=max(1, min(int(max_depth), 24)),
                max_nodes=max(10, min(int(max_nodes), 20000)),
                max_children=max(1, min(int(max_children), 200)),
                min_size_mb=max(0.0, float(min_size_mb)),
                min_ratio=max(0.0, min(float(min_ratio), 1.0)),
                prune_junk=bool(prune_junk),
                exclude=exclude,
                include=include,
                files=bool(files),
                timeout=max(10, min(int(timeout), 900)),
                progress=emit,
            )
        except FileNotFoundError:
            return ToolResult(status="failure", error=f"path does not exist: {target}")
        except NotADirectoryError:
            return ToolResult(status="failure", error=f"not a directory: {target}")
        except PermissionError:
            return ToolResult(status="failure", error=f"access denied: {target}")
        except OSError as e:
            return ToolResult(status="error", error=f"{type(e).__name__}: {e}")

        r = result.report

        # Park the FULL raw entry list with absolute paths in the session cache.
        # The context carries a filtered view, but actions ("delete that videos
        # folder") need the real paths, and they must not cost context.
        cache = LAST_SCAN_REGISTRY.record(result)

        # Two renderings, on purpose:
        #   text    - full flat listing, for the turn that just asked for it;
        #   summary - the heavy-folder tree, which is what stays in context once
        #             that turn is over, so "what should I delete?" still knows
        #             the folder names instead of getting a 3-line digest.
        compact = format_compact(result)
        return ToolResult(
            status="success",
            text=format_report(result),
            summary=compact,
            data={
                "scanned_path": os.path.abspath(target),
                "nodes_scanned": r.nodes_scanned,
                "total_bytes": r.total,
                "scan_seconds": round(r.scan_seconds, 2),
                "partial": r.partial,
                "stopped_by": r.stopped_by,
                "unreadable_dirs": r.denied,
                "hidden_entries": r.hidden_nodes,
                "hidden_bytes": r.hidden_bytes,
                "shown_entries": len(r.entries),
                "top_consumers": r.top_consumers,
                "largest_files": r.largest_files,
                "compact_text": compact,
                "cache_entries": len(cache),
                "cache_resolvable": cache.resolvable,
                "cache_file": LAST_SCAN_REGISTRY.cache_file,
                "scanner_location": scanner_location(),
            },
        )


def _throttled(sink: Callable[[dict], None] | None) -> Callable[[dict], None] | None:
    """Wrap `sink` so at most one call per interval gets through.

    The final `done` sample always passes: it is the only one carrying the true
    totals, and dropping it would leave the progress bar short of 100%.
    """
    if sink is None:
        return None

    lock = threading.Lock()
    state = {"last": -1.0}

    def emit(sample: dict) -> None:
        try:
            if not sample.get("done"):
                now = float(sample.get("elapsed") or 0.0)
                with lock:
                    if state["last"] >= 0.0 and now - state["last"] < _PROGRESS_MIN_INTERVAL:
                        return
                    state["last"] = now
            sink(sample)
        except Exception:
            # Progress is decoration; a broken sink must not fail the scan.
            pass

    return emit
