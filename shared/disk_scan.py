"""Format the native disk scan into output a language model can actually read.

The walk, the subtree aggregation and the prune/collapse/budget selection all
live in `jarvis_disk`, a Rust extension module loaded in-process through PyO3 -
no subprocess and no temporary JSON file. What arrives here is already bounded
to a few hundred entries.

The report is deliberately **flat**: one absolute path and one size per line.
A box-drawing tree costs more characters than the information is worth once the
paths repeat themselves, and a JSON blob costs even more. Anything the native
side folded away is still reported back, so the model can tell the difference
between "there is nothing there" and "it was not shown".
"""
from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("disk_scan")

_GB = 1024 ** 3

# Wall-clock budget for the whole report, header and footer included.
_MAX_CHARS = 14000

# Budget for the context-safe digest kept across turns (`format_compact`).
# ~5k characters is ~1.5k tokens: enough for every heavy folder name, small
# enough to be resent on every later request without dominating the window.
_COMPACT_CHARS = 5000

_HOW_TO_SEE_MORE = (
    "Hidden data is NOT absence of data. To see more: raise max_depth, "
    "max_nodes or max_children, lower min_size_mb or min_ratio, or pass "
    "prune_junk=false to keep the built-in junk list."
)

# Entry kinds, mirrored from the Rust side.
_FILE, _DIR, _COLLAPSED, _HIDDEN = 0, 1, 2, 3


class ScannerMissing(RuntimeError):
    """The native scanner extension is not built/importable."""


_MODULE_NAME = "jarvis_disk"
_PYD_NAME = "jarvis_disk.pyd"
# Escape hatch for a machine where the module lives somewhere we would not guess.
_ENV_VAR = "JARVIS_DISK_PYD"

_loaded: object | None = None
_loaded_from: str | None = None


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def candidate_paths() -> list[Path]:
    """Every place the scanner `.pyd` may live, most specific first.

    Order matters: an explicit override beats whatever is importable, and the
    packaged app's own folder beats the source tree, so a dev checkout cannot
    shadow the binary that shipped with the installer.
    """
    out: list[Path] = []

    override = os.environ.get(_ENV_VAR)
    if override:
        out.append(Path(override))

    # PyInstaller-style one-file bundles unpack next to the exe.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        out.append(Path(meipass))

    exe = Path(sys.executable).resolve().parent if sys.executable else None
    roots: list[Path] = []
    if exe:
        roots.extend([exe, exe / "resources", exe.parent / "resources"])
    roots.append(Path.cwd())

    for root in roots:
        out.extend([root / _PYD_NAME, root / "binaries" / _PYD_NAME,
                    root / "Jarvis" / _PYD_NAME, root / "lib" / _PYD_NAME])

    root = _project_root()
    out.extend([root / _PYD_NAME, root / "src-tauri" / "binaries" / _PYD_NAME])

    seen: set[str] = set()
    unique: list[Path] = []
    for path in out:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _load_extension(path: Path):
    """Import a `.pyd` by absolute path, bypassing sys.path and the CWD."""
    import importlib.machinery
    import importlib.util

    loader = importlib.machinery.ExtensionFileLoader(_MODULE_NAME, str(path))
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, str(path), loader=loader)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    # Register first: a module that imports itself during init must resolve.
    previous = sys.modules.get(_MODULE_NAME)
    sys.modules[_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if previous is not None:
            sys.modules[_MODULE_NAME] = previous
        else:
            sys.modules.pop(_MODULE_NAME, None)
        raise
    return module


def load_scanner():
    """Import the native module, or explain exactly how to build it.

    Three tiers, in order: an already-importable module (dev), an explicit
    `JARVIS_DISK_PYD` override, then the packaged locations - including the
    Tauri resource folder, which is where the installer puts the file.
    """
    global _loaded, _loaded_from
    if _loaded is not None:
        return _loaded

    try:
        import jarvis_disk  # noqa: PLC0415
    except ImportError:
        pass
    else:
        _loaded = jarvis_disk
        _loaded_from = getattr(jarvis_disk, "__file__", "<builtin>")
        return _loaded

    tried: list[str] = []
    for path in candidate_paths():
        if not path.is_file():
            continue
        tried.append(str(path))
        try:
            module = _load_extension(path)
        except Exception as e:
            # A stale .pyd from another Python build lands here; keep looking
            # instead of failing the whole tool.
            log.warning("[disk_scan] %s failed to load: %s", path, e)
            continue
        _loaded = module
        _loaded_from = str(path)
        log.info("[disk_scan] loaded native scanner from %s", path)
        return _loaded

    raise ScannerMissing(
        "Native disk scanner (jarvis_disk) is not available. Build it with:\n"
        "  powershell -ExecutionPolicy Bypass -File scripts/build_disk_scan.ps1\n"
        "It produces jarvis_disk.pyd next to the project root and in "
        "src-tauri/binaries (the folder bundled into the installer). Set "
        f"{_ENV_VAR}=<path to jarvis_disk.pyd> to point at a copy elsewhere.\n"
        f"Looked in: {', '.join(str(p) for p in candidate_paths()[:6])}"
        + (f"; loaded candidates: {', '.join(tried)}" if tried else "")
    )


def scanner_location() -> str:
    """Where the native module came from, for diagnostics and for `data`."""
    if _loaded_from:
        return _loaded_from
    try:
        load_scanner()
    except ScannerMissing:
        return ""
    return _loaded_from or ""


def _human(size: float) -> str:
    if size >= _GB:
        return f"{size / _GB:.2f} GB"
    if size >= 1024 ** 2:
        return f"{size / 1024 ** 2:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{int(size)} B"


@dataclass
class Scan:
    """The native report plus the limits it was produced under."""

    report: object
    max_depth: int
    max_nodes: int
    max_children: int
    min_size_mb: float
    min_ratio: float = 0.02

    @property
    def root(self) -> str:
        return self.report.root

    @property
    def total(self) -> int:
        return self.report.total

    @property
    def nodes_scanned(self) -> int:
        return self.report.nodes_scanned


def scan(
    path: str,
    *,
    max_depth: int = 4,
    max_nodes: int = 500,
    max_children: int = 8,
    min_size_mb: float = 0.0,
    min_ratio: float = 0.02,
    prune_junk: bool = True,
    exclude: list[str] | None = None,
    include: list[str] | None = None,
    files: bool = True,
    timeout: int = 120,
    progress: Callable[[dict], None] | None = None,
) -> Scan:
    """Run the native scan. Raises OSError subclasses for a bad path.

    `progress`, when given, is called from the native worker threads while the
    walk runs. The GIL is released for the walk itself, so the caller's thread
    does not freeze the interpreter; the callback is only invoked from the
    native side, so it must not block.
    """
    native = load_scanner().scan_disk(
        path,
        max_depth=max_depth,
        max_nodes=max_nodes,
        max_children=max_children,
        min_size_mb=min_size_mb,
        min_ratio=min_ratio,
        prune_junk=prune_junk,
        files=files,
        exclude=exclude,
        include=include,
        timeout=timeout,
        progress=progress,
    )
    return Scan(
        report=native,
        max_depth=max_depth,
        max_nodes=max_nodes,
        max_children=max_children,
        min_size_mb=min_size_mb,
        min_ratio=min_ratio,
    )


def _absolute_paths(entries: list) -> list[str]:
    """Resolve every entry's absolute path from its parent chain.

    Entries arrive in DFS order with `parent` pointing at the enclosing entry, so
    a single forward pass is enough: a parent is always resolved before its
    children.
    """
    paths: list[str] = []
    for idx, entry in enumerate(entries):
        if idx == 0 or entry.parent < 0:
            paths.append(entry.name)
        else:
            paths.append(f"{paths[entry.parent]}\\{entry.name}")
    return paths


def format_report(scan: Scan) -> str:
    """Render the native report as flat text: one absolute path and size a line."""
    r = scan.report
    out: list[str] = [
        f"DISK SCAN  {r.root}",
        f"{r.nodes_scanned:,} nodes in {r.scan_seconds:.2f}s, "
        f"{_human(r.total)} total   limits: depth<={scan.max_depth}, "
        f"<={scan.max_nodes} shown, >={scan.min_size_mb:g} MB, "
        f"min_ratio={scan.min_ratio:g}, top {scan.max_children}/dir",
    ]
    if r.denied:
        out[0] += f"   ({r.denied} dirs unreadable)"
    if r.stopped_by == "node_limit":
        out[0] += f"   PARTIAL: cut at the {r.node_limit:,} node cap"
    elif r.stopped_by == "timeout":
        out[0] += "   PARTIAL: out of time, the listing is incomplete"

    if r.top_consumers:
        out.append("")
        out.append("TOP CONSUMERS")
        for name, size in r.top_consumers:
            out.append(f"  {_human(size):>10}  {name}")

    if r.largest_files:
        out.append("")
        out.append("LARGEST FILES")
        for path_, size in r.largest_files:
            out.append(f"  {_human(size):>10}  {path_}")

    entries = list(r.entries)
    if not entries:
        out.append("")
        out.append("Empty: no nodes found.")
        return "\n".join(out)

    paths = _absolute_paths(entries)
    lines: list[str] = [f"{paths[0]}  [{_human(r.total)}]"]
    for idx in range(1, len(entries)):
        e = entries[idx]
        if e.kind == _HIDDEN:
            # Synthetic: everything this folder held that fell under min_ratio.
            lines.append(f"[HIDDEN: {paths[e.parent]}: {_human(e.size)} summed under min_ratio]")
            continue
        tag = "  [collapsed]" if e.kind == _COLLAPSED else ""
        lines.append(f"{paths[idx]}  [{_human(e.size)}]{tag}")

    out.append("")
    out.append("PATHS")
    # The listing is the elastic part: cap it so header + listing + footer fit.
    reserve = 700
    budget = max(600, _MAX_CHARS - len("\n".join(out)) - 2 - reserve)
    used = 0
    kept: list[str] = []
    for line in lines:
        cost = len(line) + 1
        if used + cost > budget:
            break
        kept.append(line)
        used += cost
    dropped = len(lines) - len(kept)
    if dropped:
        out.append("")
        out.append(f"... {dropped:,} more paths not printed (char budget)")
    out.extend(kept)

    out.append("")
    bits = [f"SHOWN {len(entries):,} of {r.nodes_scanned:,} nodes "
            f"({100 * len(entries) / max(r.nodes_scanned, 1):.1f}%)"]
    if r.hidden_nodes:
        bits.append(f"{r.hidden_nodes:,} entries / {_human(r.hidden_bytes)} under the "
                    f"{scan.min_ratio:g} ratio")
    if r.pruned_nodes:
        bits.append(f"{r.pruned_nodes:,} nodes / {_human(r.pruned_bytes)} in pruned junk dirs")
    if r.collapsed_nodes:
        bits.append(
            f"{r.collapsed_nodes:,} nodes / {_human(r.collapsed_bytes)} inside collapsed dirs"
        )
    if r.budget_skipped:
        bits.append(f"{r.budget_skipped:,} siblings beyond the per-directory cap")
    if r.largest_pruned:
        worst = ", ".join(f"{n} {_human(s)}" for n, s in r.largest_pruned[:5])
        bits.append(f"largest pruned: {worst}")
    out.append(" | ".join(bits))
    out.append(_HOW_TO_SEE_MORE)
    return "\n".join(out)


def summarize(scan: Scan, max_chars: int = 700) -> str:
    """Short deterministic digest of a scan, for history and for the UI.

    No model call: this runs on every turn, so it has to be free. It keeps the
    numbers a later turn usually needs (total, biggest consumers, biggest files)
    and drops the path listing, which is what actually burns the context.
    """
    r = scan.report
    bits = [f"{r.root}: {_human(r.total)}, {r.nodes_scanned:,} nodes, {r.scan_seconds:.2f}s"]
    if r.top_consumers:
        top = ", ".join(f"{n} {_human(s)}" for n, s in r.top_consumers[:5])
        bits.append(f"top: {top}")
    if r.largest_files:
        big = ", ".join(f"{p} {_human(s)}" for p, s in r.largest_files[:3])
        bits.append(f"largest: {big}")
    if r.hidden_nodes:
        bits.append(f"{r.hidden_nodes:,} entries / {_human(r.hidden_bytes)} under "
                    f"min_ratio={scan.min_ratio:g}")
    if r.pruned_nodes:
        bits.append(f"pruned junk: {r.pruned_nodes:,} nodes / {_human(r.pruned_bytes)}")
    if r.stopped_by:
        bits.append(f"PARTIAL ({r.stopped_by})")
    text = "; ".join(bits)
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def format_compact(scan: Scan, max_chars: int = _COMPACT_CHARS) -> str:
    """The flat tree, trimmed to what a later turn can still act on.

    This is what survives in the model context once the turn that produced it is
    over, so it trades the per-file detail for the *names of the heavy folders*:
    a 3-line digest (`summarize`) makes the model lose `Videos`/`Downloads` and
    then answer "what should I delete?" with generic advice instead of a path.
    The full listing for the current turn is `format_report`; this one fits in
    ~1.5k tokens and keeps the structure visible.
    """
    r = scan.report
    raw = list(r.entries)
    out: list[str] = [
        f"DISK SCAN {r.root} — {_human(r.total)}, "
        f"{r.nodes_scanned:,} nodes, min_ratio={scan.min_ratio:g}"
    ]
    if r.stopped_by:
        out.append(f"(PARTIAL: {r.stopped_by}; the listing below is incomplete)")

    if not raw:
        return "\n".join(out)

    paths = _absolute_paths(raw)
    # Directories only: files are noise for "what is heavy here", and the raw
    # listing already carries the exact big files for this turn.
    #
    # Depth-first, which the native side emits biggest-child-first, so the
    # listing already leads with the heavy folders and the indentation always
    # shows a real parent above. A flat "sort by size" would be cheaper to read
    # but would put a child before its parent, and the model needs the nesting to
    # reason about "what is inside Videos".
    lines = [f"{paths[0]}  [{_human(r.total)}]"]
    for idx in range(1, len(raw)):
        e = raw[idx]
        if e.kind == _HIDDEN:
            lines.append(f"{'  ' * _depth_of(paths[idx], paths[0])}"
                         f"[{_human(e.size)} under the ratio]")
            continue
        if e.kind == _FILE:
            continue
        depth = _depth_of(paths[idx], paths[0])
        full_path = paths[idx]
        lines.append(f"{'  ' * depth}{full_path}  [{_human(e.size)}]")

    reserve = 260
    budget = max(400, max_chars - len("\n".join(out)) - 1 - reserve)
    used = 0
    kept: list[str] = []
    for line in lines[1:]:
        cost = len(line) + 1
        if used + cost > budget:
            break
        kept.append(line)
        used += cost
    out.extend(kept)

    dropped = len(lines) - 1 - len(kept)
    if dropped:
        out.append(f"... and {dropped} smaller folders not shown (char budget)")
    if r.hidden_nodes:
        out.append(f"{r.hidden_nodes:,} entries / {_human(r.hidden_bytes)} hidden "
                   f"under min_ratio={scan.min_ratio:g}")
    out.append("Heavy folders above, nested by indentation. All paths shown are FULL ABSOLUTE PATHS.")
    return "\n".join(out)


def _depth_of(path: str, root: str) -> int:
    """Levels below the scan root, clamped at 0 for the root itself."""
    rel = path[len(root):] if path.lower().startswith(root.lower()) else path
    return max(0, rel.count("\\"))
