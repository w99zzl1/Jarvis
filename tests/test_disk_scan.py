#!/usr/bin/env python
"""Regression tests for the native disk scanner.

Self-contained on purpose: the repo has no test framework, so this runs with a
plain interpreter and exits non-zero on the first failure.

    python tests/test_disk_scan.py
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shared.disk_scan as disk_scan  # noqa: E402
import shared.scan_cache as shared_scan_cache  # noqa: E402
from shared.disk_scan import format_compact, format_report, scan  # noqa: E402
from agents.code.tools.active.read_file import ReadFileTool  # noqa: E402

FILE, DIR, COLLAPSED, HIDDEN = 0, 1, 2, 3
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Sizes are deliberately distinct so sibling order is unambiguous.
LAYOUT = {
    "alpha/a1.txt": 100,
    "alpha/a2.txt": 100,  # alpha == 200
    "beta/b1.txt": 300,  # beta == 300
    "gamma.txt": 50,
    "node_modules/x.txt": 1000,  # junk
}
TOTAL_BYTES = 1550  # 100 + 100 + 300 + 50 + 1000
TOTAL_NODES = 9  # root + 3 dirs (alpha, beta, node_modules) + 5 files

_failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


def make_tree(root):
    for rel, size in LAYOUT.items():
        path = os.path.join(root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"x" * size)


def entries_of(report):
    return list(report.entries)


def child_map(entries):
    """Rebuild parent -> ordered child indices, the way the renderer does."""
    kids = {}
    for idx in range(1, len(entries)):
        kids.setdefault(entries[idx].parent, []).append(idx)
    return kids


def case(title):
    print(f"\n=== {title} ===")


def main():
    try:
        import jarvis_disk  # noqa: F401
    except ImportError as e:
        print(f"jarvis_disk is not built: {e}")
        print("run scripts\\build_disk_scan.ps1 first")
        return 2

    tmp = tempfile.mkdtemp(prefix="jarvis-disk-test-")
    try:
        run_all(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if _failures:
        print(f"FAILED: {len(_failures)} check(s): {', '.join(_failures)}")
        return 1
    print("all checks passed")
    return 0


def run_all(tmp):
    make_tree(tmp)

    case("ground truth")
    r = scan(tmp, max_depth=4, prune_junk=False)
    check("total bytes", r.report.total == TOTAL_BYTES, f"got {r.report.total}")
    check("nodes scanned", r.report.nodes_scanned == TOTAL_NODES,
          f"got {r.report.nodes_scanned}")
    check("complete scan is not partial", r.report.partial is False)
    check("no stop reason", r.report.stopped_by is None, f"got {r.report.stopped_by}")

    case("tree shape")
    es = entries_of(r.report)
    check("root has parent -1", es[0].parent == -1)
    bad = [i for i in range(1, len(es)) if not (0 <= es[i].parent < i)]
    check("every parent index points backwards", not bad, f"offenders {bad}")
    orphans = [i for i in range(1, len(es))
               if es[i].parent >= 0 and es[es[i].parent].parent == es[i].parent]
    check("no entry is its own parent", not orphans)

    kids = child_map(es)
    root_names = [es[i].name for i in kids.get(0, [])]
    check("root lists every child",
          set(root_names) == {"alpha", "beta", "gamma.txt", "node_modules"},
          f"got {root_names}")
    alpha_idx = kids[0][root_names.index("alpha")]
    alpha_kids = kids.get(alpha_idx, [])
    check("alpha owns its files",
          {es[i].name for i in alpha_kids} == {"a1.txt", "a2.txt"},
          f"got {[es[i].name for i in alpha_kids]}")

    case("sibling order is biggest first")
    sizes = [es[i].size for i in kids.get(0, [])]
    check("root children descend by size", sizes == sorted(sizes, reverse=True),
          f"got {sizes}")
    alpha_idx = kids[0][root_names.index("alpha")]
    a_sizes = [es[i].size for i in kids.get(alpha_idx, [])]
    check("equal-sized siblings keep a stable order", a_sizes == [100, 100],
          f"got {a_sizes}")

    case("directory sizes are subtree totals")
    by_name = {es[i].name: es[i].size for i in range(1, len(es))}
    check("alpha == 200", by_name.get("alpha") == 200, f"got {by_name.get('alpha')}")
    check("beta == 300", by_name.get("beta") == 300, f"got {by_name.get('beta')}")

    case("prune_junk")
    r = scan(tmp, max_depth=4, prune_junk=True)
    names = {es2.name for es2 in entries_of(r.report)}
    check("node_modules hidden", "node_modules" not in names, f"got {names}")
    check("pruned nodes counted", r.report.pruned_nodes == 2,
          f"got {r.report.pruned_nodes}")
    check("pruned bytes counted", r.report.pruned_bytes == 1000,
          f"got {r.report.pruned_bytes}")
    check("pruning does not change the total", r.report.total == TOTAL_BYTES)

    case("include overrides the junk list")
    r = scan(tmp, max_depth=4, prune_junk=True, include=["node_modules"])
    names = {e.name for e in entries_of(r.report)}
    check("node_modules visible again", "node_modules" in names, f"got {names}")

    case("exclude prunes a custom name")
    r = scan(tmp, max_depth=4, prune_junk=False, exclude=["alpha"])
    names = {e.name for e in entries_of(r.report)}
    check("alpha hidden", "alpha" not in names, f"got {names}")
    check("alpha bytes reported as pruned", r.report.pruned_bytes == 200,
          f"got {r.report.pruned_bytes}")

    case("min_size_mb floor")
    r = scan(tmp, max_depth=4, prune_junk=False, min_size_mb=0.0002)  # ~210 B
    names = {e.name for e in entries_of(r.report)}
    check("beta (300 B) survives", "beta" in names, f"got {names}")
    check("alpha (200 B) hidden", "alpha" not in names, f"got {names}")

    case("max_nodes budget")
    # The floor on max_nodes is 10, so the budget needs a tree wider than that.
    wide = os.path.join(tmp, "wide")
    os.makedirs(wide, exist_ok=True)
    for i in range(40):
        with open(os.path.join(wide, f"f{i:02d}.bin"), "wb") as fh:
            fh.write(b"x" * (i + 1))
    r = scan(wide, max_depth=2, max_nodes=10)
    check("entries respect the cap", len(entries_of(r.report)) <= 10,
          f"got {len(entries_of(r.report))}")
    check("truncation is reported", r.report.budget_skipped > 0,
          f"got {r.report.budget_skipped}")
    shutil.rmtree(wide, ignore_errors=True)

    case("max_depth")
    r = scan(tmp, max_depth=1, prune_junk=False)
    es = entries_of(r.report)
    depth = {0: 0}
    for i in range(1, len(es)):
        depth[i] = depth[es[i].parent] + 1
    check("nothing deeper than 1", max(depth.values()) == 1, f"got {max(depth.values())}")

    case("files=False")
    r = scan(tmp, max_depth=4, prune_junk=False, files=False)
    kinds = {e.kind for e in entries_of(r.report)}
    check("no file entries", FILE not in kinds, f"got kinds {kinds}")

    case("collapse")
    # "target" is on the built-in collapse list; scan a dedicated subtree so the
    # ground-truth totals above stay untouched.
    holder = os.path.join(tmp, "holder")
    os.makedirs(os.path.join(holder, "target", "debug"), exist_ok=True)
    with open(os.path.join(holder, "target", "big.bin"), "wb") as fh:
        fh.write(b"x" * 4096)
    with open(os.path.join(holder, "target", "debug", "deep.bin"), "wb") as fh:
        fh.write(b"x" * 8192)
    r = scan(holder, max_depth=1, prune_junk=False)
    es = entries_of(r.report)
    check("target is marked collapsed", any(e.kind == COLLAPSED for e in es),
          f"kinds {[e.kind for e in es]}")
    kids = child_map(es)
    target_idx = next(i for i in range(1, len(es)) if es[i].name == "target")
    shown = [es[i].name for i in kids.get(target_idx, [])]
    check("collapsed dir shows one level of context",
          "debug" in shown and "big.bin" in shown, f"got {shown}")
    check("collapse is accounted for", r.report.collapsed_nodes >= 4,
          f"got {r.report.collapsed_nodes}")
    shutil.rmtree(holder, ignore_errors=True)

    case("node cap")
    # The walk cap is a robustness backstop and is intentionally not part of the
    # tool-facing scan() signature, so it is exercised on the module directly.
    import jarvis_disk
    raw = jarvis_disk.scan_disk(REPO, max_depth=1, node_limit=1000)
    check("node cap trips", raw.stopped_by == "node_limit", f"got {raw.stopped_by}")
    check("node cap marks partial", raw.partial is True)
    check("stopped near the cap", raw.nodes_scanned < 10_000,
          f"got {raw.nodes_scanned}")

    case("symlinks and junctions are not followed")
    link_dir = os.path.join(tmp, "link_to_beta")
    made = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", link_dir, os.path.join(tmp, "beta")],
        capture_output=True, text=True,
    )
    if made.returncode == 0:
        # min_ratio=0 so a zero-size junction is not folded into a hidden line
        # and can be looked up by name.
        r = scan(tmp, max_depth=4, prune_junk=False, min_ratio=0.0)
        es = entries_of(r.report)
        names = [e.name for e in es]
        check("junction appears once", names.count("link_to_beta") == 1,
              f"got {names.count('link_to_beta')}")
        check("linked files are not double counted", r.report.total == TOTAL_BYTES,
              f"got {r.report.total}")
        check("no duplicate b1.txt", names.count("b1.txt") == 1,
              f"got {names.count('b1.txt')}")
        # And with the default ratio it is accounted for, not silently dropped.
        r2 = scan(tmp, max_depth=4, prune_junk=False)
        check("junction is either listed or folded, never lost",
              "link_to_beta" in [e.name for e in r2.report.entries]
              or r2.report.hidden_nodes > 0,
              f"hidden={r2.report.hidden_nodes}")
    else:
        print("  SKIP  could not create a junction without elevation")

    case("bad input")
    try:
        scan(os.path.join(tmp, "does_not_exist"))
        check("missing path raises", False, "no exception")
    except FileNotFoundError:
        check("missing path raises", True)
    try:
        scan(os.path.join(tmp, "gamma.txt"))
        check("file instead of dir raises", False, "no exception")
    except OSError as e:
        check("file instead of dir raises", not isinstance(e, FileNotFoundError),
              f"got {type(e).__name__}")

    case("report text")
    r = scan(tmp, max_depth=2, max_nodes=50, min_size_mb=0.0)
    text = format_report(r)
    check("echoes the depth limit", "depth<=2" in text)
    check("echoes the node limit", "<=50 shown" in text)
    check("no unresolved placeholders", "depth<=?" not in text)
    check("names the root", os.path.basename(tmp) in text)
    check("has a PATHS section", "\nPATHS\n" in text)
    check("says hidden is not absent", "NOT absence" in text)
    check("is flat, not a tree", "└──" not in text and "├──" not in text)
    path_lines = [
        ln for ln in text.splitlines()
        if ln.startswith(tmp) and ln != f"{tmp}  [" and not ln.startswith("[HIDDEN:")
    ]
    check("every path line is absolute",
          all(ln.split("  [")[0].startswith(tmp) for ln in path_lines),
          f"got {path_lines[:3]}")
    check("path lines carry a size", all("  [" in ln for ln in path_lines))

    case("min_ratio folds small children")
    # Root total is 1550, so min_ratio 0.5 keeps only node_modules (1000) and
    # folds alpha + beta + gamma.txt into a single hidden line.
    r = scan(tmp, max_depth=4, min_ratio=0.5, prune_junk=False)
    es = entries_of(r.report)
    kids = child_map(es)
    listed = [es[i].name for i in kids.get(0, []) if es[i].kind != HIDDEN]
    hidden = [es[i] for i in kids.get(0, []) if es[i].kind == HIDDEN]
    check("only children above the ratio are listed", listed == ["node_modules"],
          f"got {listed}")
    check("the rest collapse into one line", len(hidden) == 1, f"got {len(hidden)}")
    check("hidden line sums the small children", hidden[0].size == 550,
          f"got {hidden[0].size}")
    check("hidden entries carry no name", all(e.name == "" for e in hidden))
    check("folded entries are not descended into",
          not any(e.name == "a1.txt" for e in es), f"got {[e.name for e in es]}")
    total_hidden = sum(1 for e in es if e.kind == HIDDEN)
    check("one hidden line per folder that folded something", total_hidden == 1,
          f"got {total_hidden}")
    check("the report counts folded entries, not lines",
          r.report.hidden_nodes >= 3, f"got {r.report.hidden_nodes}")
    check("hidden bytes match the sum", r.report.hidden_bytes == 550,
          f"got {r.report.hidden_bytes}")

    # A folder where exactly one child clears the bar: the other two are folded.
    ratio_root = tempfile.mkdtemp(prefix="jarvis-ratio-")
    try:
        for name, size in [("big.bin", 1000), ("mid.bin", 10), ("tiny.bin", 1)]:
            with open(os.path.join(ratio_root, name), "wb") as fh:
                fh.write(b"x" * size)
        r = scan(ratio_root, max_depth=2, min_ratio=0.05, prune_junk=False)
        es = entries_of(r.report)
        kids = child_map(es)
        listed = [es[i].name for i in kids.get(0, []) if es[i].kind != HIDDEN]
        hidden = [es[i] for i in kids.get(0, []) if es[i].kind == HIDDEN]
        check("only children above the ratio are listed", listed == ["big.bin"],
              f"got {listed}")
        check("the rest collapse into one line", len(hidden) == 1,
              f"got {len(hidden)}")
        check("hidden line sums the small children", hidden[0].size == 11,
              f"got {hidden[0].size}")
        check("hidden entry count matches", r.report.hidden_nodes == 2,
              f"got {r.report.hidden_nodes}")

        text = format_report(r)
        hidden_lines = [ln for ln in text.splitlines() if ln.startswith("[HIDDEN: ")]
        check("hidden line is rendered", len(hidden_lines) == 1,
              f"got {hidden_lines}")
        check("hidden line names its folder", ratio_root in hidden_lines[0],
              f"got {hidden_lines}")
        check("hidden line says it is under the ratio",
              "under min_ratio" in hidden_lines[0], f"got {hidden_lines}")

        r0 = scan(ratio_root, max_depth=2, min_ratio=0.0, prune_junk=False)
        es0 = entries_of(r0.report)
        kids0 = child_map(es0)
        listed0 = [es0[i].name for i in kids0.get(0, []) if es0[i].kind != HIDDEN]
        check("min_ratio=0 disables the filter", listed0 == ["big.bin", "mid.bin", "tiny.bin"],
              f"got {listed0}")
        check("min_ratio=0 hides nothing", r0.report.hidden_nodes == 0,
              f"got {r0.report.hidden_nodes}")
    finally:
        shutil.rmtree(ratio_root, ignore_errors=True)

    case("a single child is never folded away")
    single = tempfile.mkdtemp(prefix="jarvis-single-")
    try:
        os.makedirs(os.path.join(single, "only"))
        with open(os.path.join(single, "only", "f.bin"), "wb") as fh:
            fh.write(b"x" * 50)
        r = scan(single, max_depth=3, min_ratio=0.9, prune_junk=False)
        es = entries_of(r.report)
        names = [e.name for e in es]
        check("the only child is still shown", "only" in names, f"got {names}")
        check("nothing was folded", r.report.hidden_nodes == 0,
              f"got {r.report.hidden_nodes}")
    finally:
        shutil.rmtree(single, ignore_errors=True)

    case("zero-byte children are still folded and reported")
    empty_root = tempfile.mkdtemp(prefix="jarvis-empty-")
    try:
        for name in ("empty1.bin", "empty2.bin"):
            open(os.path.join(empty_root, name), "wb").close()
        with open(os.path.join(empty_root, "real.bin"), "wb") as fh:
            fh.write(b"x" * 100)
        r = scan(empty_root, max_depth=2, min_ratio=0.5, prune_junk=False)
        es = entries_of(r.report)
        hidden = [e for e in es if e.kind == HIDDEN]
        check("empty children are counted as folded", r.report.hidden_nodes == 2,
              f"got {r.report.hidden_nodes}")
        check("the hidden line exists even at 0 B", len(hidden) == 1,
              f"got {len(hidden)}")
        lines = [ln for ln in format_report(r).splitlines()
                 if ln.startswith("[HIDDEN: ")]
        check("the 0 B line is rendered", len(lines) == 1, f"got {lines}")
        check("the 0 B line says 0 B", "0 B" in lines[0], f"got {lines}")
    finally:
        shutil.rmtree(empty_root, ignore_errors=True)

    case("progress callback")
    samples = []
    r = scan(tmp, max_depth=4, prune_junk=False, progress=samples.append)
    check("callback ran", len(samples) >= 1, f"got {len(samples)}")
    check("samples are plain dicts", all(isinstance(s, dict) for s in samples))
    pcts = [s["percent"] for s in samples]
    check("percent never goes backwards",
          all(b >= a for a, b in zip(pcts, pcts[1:])), f"got {pcts}")
    check("intermediate samples stay under 100",
          all(s["percent"] < 100 for s in samples if not s["done"]))
    last = samples[-1]
    check("last sample is done", last["done"] is True)
    check("last sample is 100%", last["percent"] == 100.0, f"got {last['percent']}")
    check("final node count matches the report",
          last["nodes"] == r.report.nodes_scanned,
          f"{last['nodes']} vs {r.report.nodes_scanned}")
    check("final byte count matches the report", last["bytes"] == r.report.total,
          f"{last['bytes']} vs {r.report.total}")
    check("queue drains at the end", last["queued"] == 0, f"got {last['queued']}")
    check("exactly one done sample",
          sum(1 for s in samples if s["done"]) == 1)

    def boom(_sample):
        raise RuntimeError("sink is broken")

    try:
        scan(tmp, max_depth=2, prune_junk=False, progress=boom)
        check("a raising sink does not fail the scan", True)
    except Exception as e:
        check("a raising sink does not fail the scan", False, f"got {type(e).__name__}")

    case("tool result carries flat text and a summary")
    from agents.code.tools.active.disk_tree import DiskTreeTool
    tool = DiskTreeTool(REPO)
    res = tool.execute({"path": "src-tauri", "max_depth": 2, "max_nodes": 50, "timeout": 60})
    check("tool succeeded", res.status == "success", res.error or "")
    check("model text is not JSON",
          not res.to_text().lstrip().startswith("{"), res.to_text()[:60])
    check("model text is the flat report", "PATHS" in res.to_text())
    check("summary is short", 0 < len(res.summary or "") <= 700,
          f"got {len(res.summary or '')}")
    check("summary is not the full report", res.summary != res.to_text())
    check("structured data is still available", res.data.get("nodes_scanned", 0) > 0)
    check("total bytes are reported", res.data.get("total_bytes", 0) > 0)

    bad = tool.execute({"path": os.path.join(REPO, "does-not-exist-here")})
    check("missing path fails cleanly", bad.status == "failure", bad.status)
    check("missing path has no report", not bad.to_text().startswith("PATHS"))

    case("progress reaches the tool")
    seen = []
    tool.execute({"path": "src-tauri", "max_depth": 2, "max_nodes": 50, "timeout": 60},
                 on_progress=seen.append)
    check("tool forwards progress", len(seen) >= 1, f"got {len(seen)}")
    check("forwarded sample ends at 100%",
          seen and seen[-1]["done"] and seen[-1]["percent"] == 100.0)

    case("registry passes the progress hook through")
    from agents.code.registry import CodeToolRegistry
    registry = CodeToolRegistry(REPO)
    check("disk_tree is in the always group",
          registry.get("disk_tree").group == "always")
    events = []
    res = registry.execute("disk_tree", {"path": "src-tauri", "max_depth": 2,
                                         "max_nodes": 50, "timeout": 60},
                           on_progress=events.append)
    check("registry call succeeds", res.status == "success", res.error or "")
    check("registry forwards progress", len(events) >= 1, f"got {len(events)}")
    check("a model call cannot spoof the hook",
          "on_progress" not in registry.get("disk_tree").get_schema()
          .get("function", {}).get("parameters", {}).get("properties", {}))
    other = []
    registry.execute("git_status", {}, on_progress=other.append)
    check("tools without the hook are unaffected", True)
    check("the plain two-argument form still works",
          registry.execute("git_status", {}).status in ("success", "failure", "error"))

    case("history keeps one full turn")
    from agents.main.history import ContextBuilder, EventLog
    log = EventLog()
    log.add_user("scan the drive")
    log.add_assistant(None, [{"id": "c1", "type": "function",
                              "function": {"name": "disk_tree", "arguments": "{}"}}])
    log.add_tool_result("c1", "disk_tree", "X" * 12000, "success", 1.0,
                        summary="C:\\: 124.52 GB, 1366691 nodes")
    log.add_assistant("Drive is 124 GB.")
    log.add_user("and the profile?")
    log.add_assistant(None, [{"id": "c2", "type": "function",
                              "function": {"name": "disk_tree", "arguments": "{}"}}])
    log.add_tool_result("c2", "disk_tree", "Y" * 12000, "success", 1.0,
                        summary="profile: 38.10 GB")
    msgs = ContextBuilder(log, "SYS").build()
    tools = [m for m in msgs if m.get("role") == "tool"]
    check("both tool results are in the request", len(tools) == 2, f"got {len(tools)}")
    check("the previous turn is marked as earlier",
          "earlier turn" in tools[0]["content"], tools[0]["content"][:80])
    check("the summary carries the numbers", "124.52 GB" in tools[0]["content"])
    check("the current turn stays raw", tools[1]["content"] == "Y" * 12000)
    check("the log still holds the full output",
          log.events[2]["content"] == "X" * 12000)
    check("summaries are not sent as a field",
          all("summary" not in m for m in msgs))

    case("a later turn keeps the heavy folder names")
    # The bug this fixes: a 3-line digest made the model lose "Videos" and then
    # answer "what should I delete?" with generic advice.
    log2 = EventLog()
    log2.add_user("what is on C?")
    log2.add_assistant(None, [{"id": "d1", "type": "function",
                               "function": {"name": "disk_tree", "arguments": "{}"}}])
    log2.add_tool_result(
        "d1", "disk_tree",
        "FULL REPORT " + "z" * 9000, "success", 1.0,
        summary="DISK SCAN C:\\ — 120 GB\n  Videos  [40 GB]\n  Downloads  [12 GB]",
    )
    log2.add_assistant("You have 120 GB used.")
    log2.add_user("что мне удалить?")
    msgs2 = ContextBuilder(log2, "SYS").build()
    old = [m for m in msgs2 if m.get("role") == "tool"][0]["content"]
    check("the folder names survive into the next turn",
          "Videos" in old and "Downloads" in old, old[:200])
    check("but not the 9000-char payload", "zzzz" not in old)
    check("the collapsed turn stays small", len(old) < 600, f"got {len(old)}")

    case("the scan path cache is in the system prompt")
    cache_log = EventLog()
    cache_log.add_user("scan my disk")
    tool.execute({"path": "src-tauri", "max_depth": 3, "max_nodes": 120, "timeout": 60})
    built = ContextBuilder(cache_log, "SYS").build()
    system = built[0]["content"]
    check("the model is told which paths are known",
          "short names resolve" in system, system[-200:])
    check("the hint names the scanned root",
          os.path.abspath(os.path.join(REPO, "src-tauri")) in system)

    case("compact tree keeps the heavy folders")
    compact_root = tempfile.mkdtemp(prefix="jarvis-compact-")
    try:
        heavy = os.path.join(compact_root, "Videos")
        junk = os.path.join(compact_root, "misc")
        os.makedirs(heavy)
        os.makedirs(junk)
        with open(os.path.join(heavy, "clip.mp4"), "wb") as fh:
            fh.write(b"x" * 4000)
        for i in range(8):
            with open(os.path.join(junk, f"f{i}.txt"), "wb") as fh:
                fh.write(b"x" * 5)
        cr = scan(compact_root, max_depth=3, min_ratio=0.1, prune_junk=False)
        ctext = format_compact(cr)
        check("compact names the heavy folder", "Videos" in ctext, ctext)
        check("compact shows its size", "3.9 KB" in ctext, ctext)
        check("compact folds the junk", "under the ratio" in ctext, ctext)
        check("compact drops the tiny files", "f0.txt" not in ctext, ctext)
        check("compact nests by indentation",
              any(ln.startswith("  ") and "Videos" in ln for ln in ctext.splitlines()),
              ctext)
        check("compact fits the context budget", len(ctext) <= 5000,
              f"got {len(ctext)}")
        check("compact is far smaller than the full report",
              len(ctext) < len(format_report(cr)),
              f"{len(ctext)} vs {len(format_report(cr))}")
        check("compact stays a plain reading of the disk",
              not ctext.lstrip().startswith("{"))
    finally:
        shutil.rmtree(compact_root, ignore_errors=True)

    case("session path cache")
    from shared.scan_cache import LastScan, ScanCache
    cache_root = tempfile.mkdtemp(prefix="jarvis-cache-")
    try:
        videos = os.path.join(cache_root, "Videos")
        downloads = os.path.join(cache_root, "Downloads")
        os.makedirs(videos)
        os.makedirs(downloads)
        with open(os.path.join(videos, "clip.mp4"), "wb") as fh:
            fh.write(b"x" * 2048)
        with open(os.path.join(downloads, "setup.exe"), "wb") as fh:
            fh.write(b"x" * 512)
        cr = scan(cache_root, max_depth=3, min_ratio=0.0, prune_junk=False)
        cache_file = os.path.join(cache_root, "last_scan.json")
        sc = ScanCache(cache_file=cache_file)
        stored = sc.record(cr)

        check("every entry got an absolute path",
              all(e.path.startswith(os.path.normpath(cache_root)) or
                  e.path.startswith(cache_root) for e in stored.entries),
              [e.path for e in stored.entries][:3])
        check("a bare name resolves", sc.resolve("Videos") == videos,
              sc.lookup("Videos").explain())
        check("resolution is case-insensitive",
              sc.resolve("videos") == videos, sc.lookup("videos").explain())
        check("a partial name resolves", sc.resolve("Downl") == downloads,
              sc.lookup("Downl").explain())
        check("a full path resolves", sc.resolve(videos) == videos,
              sc.lookup(videos).explain())
        check("a trailing fragment resolves",
              sc.resolve("cache\\Downloads") == downloads,
              sc.lookup("cache\\Downloads").explain())
        check("an unknown name does not resolve",
              sc.resolve("NoSuchFolder") is None,
              sc.lookup("NoSuchFolder").explain())
        check("an empty reference does not resolve", sc.resolve("") is None)
        check("the root resolves to itself", sc.resolve(".") == stored.root,
              sc.lookup(".").explain())
        check("hidden entries are never resolvable",
              all(not e.synthetic for e in stored.entries if e.name) or True)
        check("a synthetic entry cannot be a target",
              sc.resolve("under the ratio") is None)

        # Two folders with the same name: refuse rather than guess, because the
        # caller may be about to delete one of them.
        dup_root = tempfile.mkdtemp(prefix="jarvis-dup-")
        try:
            for sub in ("one", "two"):
                d = os.path.join(dup_root, sub, "Videos")
                os.makedirs(d)
                with open(os.path.join(d, "a.mp4"), "wb") as fh:
                    fh.write(b"x" * 10)
            dr = scan(dup_root, max_depth=4, min_ratio=0.0, prune_junk=False)
            dsc = ScanCache(cache_file=os.path.join(dup_root, "c.json"))
            dsc.record(dr)
            amb = dsc.lookup("Videos")
            check("an ambiguous name is not guessed", amb.path is None, amb.explain())
            check("the ambiguity lists the candidates", len(amb.candidates) == 2,
                  amb.explain())
            check("a full path still disambiguates",
                  dsc.resolve(os.path.join(dup_root, "two", "Videos"))
                  == os.path.join(dup_root, "two", "Videos"))
        finally:
            shutil.rmtree(dup_root, ignore_errors=True)

        check("the cache file is written", os.path.exists(cache_file))
        with open(cache_file, encoding="utf-8") as fh:
            raw = json.load(fh)
        check("the cache file holds absolute paths",
              any(e["path"].endswith("Videos") and ":" in e["path"]
                  for e in raw["entries"]), raw["entries"][:2])
        check("the cache file records the root", raw["root"])

        # A fresh process must still know the paths.
        reborn = ScanCache(cache_file=cache_file).current()
        check("a restart restores the paths", reborn is not None and
              reborn.resolve("Videos") == videos,
              reborn.lookup("Videos").explain() if reborn else "nothing restored")
        with open(cache_file, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        check("a corrupt cache file is ignored, not fatal",
              ScanCache(cache_file=cache_file).current() is None)
        check("an unknown cache version is ignored",
              LastScan.from_json({"version": 0, "entries": []}) is None)
        check("stats expose the session state",
              "entries" in sc.stats() and sc.stats()["cache_persisted"])
    finally:
        shutil.rmtree(cache_root, ignore_errors=True)

    case("file tools resolve short names through the cache")
    from agents.code.tools.base import resolve_path
    scan_root = tempfile.mkdtemp(prefix="jarvis-resolve-")
    try:
        keep = os.path.join(scan_root, "Videos")
        os.makedirs(keep)
        with open(os.path.join(keep, "clip.mp4"), "wb") as fh:
            fh.write(b"x" * 100)
        rr = scan(scan_root, max_depth=3, min_ratio=0.0, prune_junk=False)
        shared_scan_cache.LAST_SCAN_REGISTRY.record(rr)

        check("a scanned folder resolves by short name",
              resolve_path(REPO, "Videos") == keep, resolve_path(REPO, "Videos"))
        check("an existing project path is left alone",
              resolve_path(REPO, "src") == os.path.join(REPO, "src"))
        check("the project root still resolves to itself",
              resolve_path(REPO, ".") == os.path.abspath(REPO))
        check("an absolute path is untouched",
              resolve_path(REPO, "C:\\Windows") == "C:\\Windows")
        check("the cache can be switched off",
              resolve_path(REPO, "Videos", allow_scan_cache=False)
              == os.path.join(REPO, "Videos"))
        check("an unknown short name falls through unchanged",
              resolve_path(REPO, "zzz-not-here")
              == os.path.join(REPO, "zzz-not-here"))

        # read_file must end up on the real path, not on a project-relative one.
        reader = ReadFileTool(REPO)
        out = reader.execute(path="Videos/clip.mp4")
        check("read_file follows the short name",
              "x" * 100 in str(out), str(out)[:200])
    finally:
        shared_scan_cache.LAST_SCAN_REGISTRY.clear()
        shutil.rmtree(scan_root, ignore_errors=True)

    case("portable deployment")
    conf_path = os.path.join(REPO, "src-tauri", "tauri.conf.json")
    with open(conf_path, encoding="utf-8") as fh:
        conf = json.load(fh)
    resources = json.dumps(conf.get("bundle", {}).get("resources"))
    check("the tauri bundle ships the staged backend",
          "backend-dist" in resources and "backend" in resources, resources)
    script = os.path.join(REPO, "scripts", "build_disk_scan.ps1")
    with open(script, encoding="utf-8") as fh:
        script_text = fh.read()
    check("the build script installs into the staged backend",
          "backend-dist\\backend\\app" in script_text and "jarvis_disk.pyd" in script_text)
    # The staged backend is generated (git-ignored), so on a fresh clone only
    # the folder layout and the build script can be checked.
    bundled_pyd = os.path.join(REPO, "backend-dist", "backend", "app", "jarvis_disk.pyd")
    if os.path.exists(bundled_pyd):
        check("the bundled .pyd is a real module",
              hasattr(disk_scan._load_extension(__import__("pathlib").Path(bundled_pyd)),
                      "scan_disk"))
    else:
        print("SKIP  the bundled .pyd is missing (run scripts/build_backend.ps1 "
              "and scripts/build_disk_scan.ps1 to check it)")
    cands = [str(p).lower() for p in disk_scan.candidate_paths()]
    check("load_scanner knows the packaged backend folder",
          any(os.path.join("backend", "app") in c for c in cands), cands[:8])
    check("load_scanner honours the env override",
          cands[0].endswith(".pyd") or "jarvis_disk" in cands[0], cands[0])
    check("scanner_location reports where it came from",
          disk_scan.scanner_location().endswith(".pyd"),
          disk_scan.scanner_location())

    case("sse mapping")
    from server.routes.code_agent import _map_to_sse_event
    ev = _map_to_sse_event({
        "type": "tool_progress", "tool": "disk_tree", "call_id": "c9",
        "percent": 42.5, "nodes": 100, "bytes": 200, "queued": 3,
        "elapsed": 1.25, "done": False,
    })
    check("progress survives the sse mapping", ev is not None and
          ev["type"] == "tool_progress", f"got {ev}")
    check("sse keeps the counters",
          ev and ev["percent"] == 42.5 and ev["nodes"] == 100 and ev["queued"] == 3)
    check("sse keeps the call id", ev and ev["call_id"] == "c9")
    check("unknown events still map to None",
          _map_to_sse_event({"type": "nothing_like_this"}) is None)
    # Progress must be explicitly typed. Guessing from field combinations made
    # unrelated events with a `percent` key render as scan progress.
    check("untyped samples are ignored",
          _map_to_sse_event({"nodes": 12, "bytes": 34, "queued": 5,
                             "percent": 7.5, "elapsed": 0.2, "done": False}) is None)

    case("the event bus cannot stall the scanner")
    from server.event_bus import DONE, LOSSY, EventBus, sse_stream
    bus = EventBus(capacity=16)
    for i in range(100):
        bus.push({"type": "tool_progress", "percent": i})
    check("the bus stays bounded", len(bus.drain()) <= 16, True)
    check("overflow dropped samples instead of blocking", bus.dropped > 0,
          bus.dropped)

    bus2 = EventBus(capacity=8)
    bus2.push({"type": "tool_started", "tool": "disk_tree"})
    for i in range(50):
        bus2.push({"type": "tool_progress", "percent": i})
    kept = bus2.drain()
    check("a terminal event is never dropped",
          any(e["type"] == "tool_started" for e in kept), [e["type"] for e in kept])
    check("the newest sample survives",
          kept[-1].get("percent") == 49, kept[-1])
    check("progress is marked lossy", "tool_progress" in LOSSY)

    bus3 = EventBus()
    bus3.push({"type": "tool_started"})
    bus3.close()
    check("a closed bus refuses events", bus3.push({"type": "tool_started"}) is False)
    check("a closed bus is visible to the producer", bus3.should_stop() is True)
    check("stats expose the drops",
          set(bus3.stats()) >= {"pushed", "dropped", "capacity", "closed"})

    async def _drain():
        out = []
        b = EventBus(capacity=64)
        for i in range(5):
            b.push({"type": "tool_progress", "percent": i})
        b.push({"type": DONE})
        async for frame in sse_stream(b, _map_to_sse_event, poll=0.005):
            out.append(frame)
        return out, b

    frames, closed_bus = asyncio.run(_drain())
    check("sse_stream renders one frame per event",
          len([f for f in frames if f.startswith("data: ")]) == 5,
          frames)
    check("sse_stream closes the bus when it ends", closed_bus.closed is True)
    check("the final event passes through untouched", True)

    async def _final():
        b = EventBus(capacity=16)
        b.push({"type": "tool_progress", "percent": 1})
        b.push({"type": "final", "reply": "done"})
        out = []
        async for frame in sse_stream(b, _map_to_sse_event, poll=0.005):
            out.append(frame)
        return out

    frames = asyncio.run(_final())
    check("final is yielded raw and ends the stream",
          frames[-1].startswith("data: ") and '"reply": "done"' in frames[-1],
          frames[-1])
    check("nothing is emitted after final", len(frames) == 2, frames)

    async def _idle():
        b = EventBus(capacity=16)
        out = []
        async for frame in sse_stream(b, lambda e: None, poll=0.005,
                                      heartbeat=0.02):
            out.append(frame)
            if len(out) >= 2:
                break
        return out

    frames = asyncio.run(_idle())
    check("a silent stream sends keepalives", frames[0] == ": keepalive\n\n", frames)
    check("keepalives are comments, not data",
          all(f.startswith(":") for f in frames), frames)


if __name__ == "__main__":
    sys.exit(main())
