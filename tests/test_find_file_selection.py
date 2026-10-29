"""Tests for find_file tool selection and real lookup.

Deterministic checks (no LLM):
  - code agent exposes find_file in the always (upfront) tool set
  - find_file resolves a real file by name, including files newer than the index

Live checks (real model, repeated):
  - ask the code agent to find SamFwToolSetup.exe
  - if it calls shell/run_powershell/run_cmd, ABORT the agent immediately and
    fail the run
  - success only when find_file is called and returns the exact path

Run:
    python tests/test_find_file_selection.py [runs]

Env:
    FIND_FILE_TEST_RUNS   number of live runs (default 3)
    FIND_FILE_TEST_MODEL  model id (default gemini-3.1-flash-lite)
    FIND_FILE_TEST_PROVIDER  provider (default google)
"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TARGET = "SamFwToolSetup.exe"
PROMPT = "Найди файл SamFwToolSetup.exe"
SHELL_TOOLS = {"shell", "run_powershell", "run_cmd"}


class ShellAbort(BaseException):
    """Raised on the agent thread to stop the whole turn when shell is chosen."""


def _build_agent(model=None, provider=None):
    from agents.code.factory import create_code_agent

    return create_code_agent(
        project_path=ROOT,
        primary_model=model or os.getenv("FIND_FILE_TEST_MODEL", "gemini-3.1-flash-lite"),
        primary_provider=provider or os.getenv("FIND_FILE_TEST_PROVIDER", "google"),
    )


def test_code_agent_lists_find_file_as_lazy():
    agent = _build_agent()
    upfront = [s["function"]["name"] for s in agent.registry.get_llm_specs(group_filter="always")]
    lazy = [t["name"] for t in agent.registry.list_available()]
    assert "find_file" not in upfront, f"find_file must stay lazy, got upfront={upfront}"
    assert "find_file" in lazy, f"find_file missing from lazy tools: {lazy}"
    prompt = agent.system_prompt
    assert "find_file" in prompt, "find_file is not described in the system prompt"
    assert "Lazy tools" in prompt, "prompt does not explain lazy tool loading"
    assert "BY NAME" in prompt.upper() or "by its name" in prompt.lower()


def test_main_agent_lists_find_file_as_lazy():
    from agents.main.factory import create_agent

    agent = create_agent()
    upfront = [s["function"]["name"] for s in agent.registry.get_llm_specs(group_filter="always")]
    lazy = [
        t.name for t in agent.registry._tools.values()
        if getattr(t, "group", "on_demand") == "on_demand"
    ]
    assert "find_file" not in upfront, f"find_file must stay lazy, got upfront={upfront}"
    assert "find_file" in lazy, f"find_file missing from lazy tools: {lazy}"
    assert "find_file" in agent.system_prompt, "find_file is not described in the system prompt"
    assert "Lazy tools" in agent.system_prompt


def test_find_file_resolves_target():
    from agents.main.tools.search_files import SearchFilesTool

    tool = SearchFilesTool()
    started = time.time()
    result = tool.execute({"query": TARGET, "path": ""})
    elapsed = time.time() - started

    assert result.success, result.error
    matches = [m["file"] for m in (result.data or {}).get("matches", [])]
    exact = [p for p in matches if os.path.basename(p).lower() == TARGET.lower()]
    assert exact, f"no exact match for {TARGET} in {matches}"
    assert os.path.isfile(exact[0]), f"returned path does not exist: {exact[0]}"
    assert elapsed < 15, f"lookup too slow: {elapsed:.1f}s"


def test_index_reports_age_and_self_heals():
    from shared import file_index

    age = file_index.index_age_seconds()
    assert age is not None and age >= 0, "index age must be known"
    assert file_index.is_stale(max_age_seconds=10**9) is False

    fake = os.path.join(ROOT, "tests", "zzz_find_file_selftest_51771.bin")
    if not os.path.isfile(file_index._INDEX_PATH):
        return
    file_index.add_paths([fake])
    matches, _data, _truncated = file_index.search("zzz_find_file_selftest_51771", limit=5)
    assert fake in matches, "add_paths did not make a newly discovered file searchable"


def run_live_once(model=None, provider=None):
    agent = _build_agent(model=model, provider=provider)

    calls = []
    find_file_results = []
    shell_calls = []
    found_path = None
    aborted = None

    original_execute = agent._execute_tool

    def guarded_execute(name, args, call_id):
        calls.append((name, args))
        if name in SHELL_TOOLS:
            shell_calls.append((name, args))
            # Abort before the shell command ever runs: no process to kill.
            raise ShellAbort(f"agent chose {name} instead of find_file: {args}")
        result = original_execute(name, args, call_id)
        if name == "find_file":
            find_file_results.append((args, result))
        return result

    agent._execute_tool = guarded_execute

    run_result = None
    started = time.time()
    try:
        run_result = agent.run(PROMPT)
    except ShellAbort as e:
        aborted = str(e)

    for _args, result in find_file_results:
        for match in (result.data or {}).get("matches", []):
            path = match.get("file") if isinstance(match, dict) else match
            if path and os.path.basename(path).lower() == TARGET.lower():
                found_path = path
                break
        if found_path:
            break

    return {
        "calls": [name for name, _ in calls],
        "shell_calls": shell_calls,
        "aborted": aborted,
        "find_file_calls": len(find_file_results),
        "found_path": found_path,
        "output": getattr(run_result, "output", None),
        "state": getattr(run_result, "state", None),
        "success": getattr(run_result, "success", False),
        "error": getattr(run_result, "error", None),
        "elapsed": round(time.time() - started, 2),
    }


def test_agent_chooses_find_file(runs=None):
    model = os.getenv("FIND_FILE_TEST_MODEL", "gemini-3.1-flash-lite")
    provider = os.getenv("FIND_FILE_TEST_PROVIDER", "google")
    if runs is None:
        runs = int(os.getenv("FIND_FILE_TEST_RUNS", "3"))

    failures = []
    for i in range(1, runs + 1):
        shot = run_live_once(model=model, provider=provider)
        print(f"\n[run {i}/{runs}] tools={shot['calls']} elapsed={shot['elapsed']}s")
        if shot["shell_calls"]:
            failures.append(
                f"run {i}: ABORTED - chose shell {shot['shell_calls']} "
                f"instead of find_file ({shot['aborted']})"
            )
        elif shot["find_file_calls"] == 0:
            failures.append(f"run {i}: find_file was never called; tools={shot['calls']}")
        elif not shot["found_path"]:
            failures.append(f"run {i}: find_file called but no exact {TARGET} path returned")
        else:
            assert os.path.isfile(shot["found_path"]), shot["found_path"]
            print(f"[run {i}/{runs}] SUCCESS -> {shot['found_path']}")

    assert not failures, "\n".join(failures)


def test_main_agent_lists_disk_tree_as_lazy():
    from agents.main.factory import create_agent

    agent = create_agent()
    upfront = [s["function"]["name"] for s in agent.registry.get_llm_specs(group_filter="always")]
    assert "disk_tree" not in upfront, f"disk_tree must stay lazy, got upfront={upfront}"
    lazy = [
        t.name for t in agent.registry._tools.values()
        if getattr(t, "group", "on_demand") == "on_demand"
    ]
    assert "disk_tree" in lazy, f"disk_tree missing from lazy tools: {lazy}"
    assert "disk_tree" in agent.system_prompt, "disk_tree not described in main agent prompt"
    assert "Lazy tools" in agent.system_prompt


def _build_main_agent(model=None, provider=None):
    from agents.main.factory import create_agent

    return create_agent(
        primary_model=model or os.getenv("FIND_FILE_TEST_MODEL", "gemini-3.1-flash-lite"),
        primary_provider=provider or os.getenv("FIND_FILE_TEST_PROVIDER", "google"),
    )


def _run_main_live_once(prompt, model=None, provider=None):
    agent = _build_main_agent(model=model, provider=provider)

    calls = []
    shell_calls = []

    original_execute = agent._execute_tool

    def guarded_execute(name, args, call_id):
        calls.append((name, args))
        if name in SHELL_TOOLS:
            shell_calls.append((name, args))
            raise ShellAbort(f"agent chose {name} instead of purpose-built tool: {args}")
        return original_execute(name, args, call_id)

    agent._execute_tool = guarded_execute

    run_result = None
    started = time.time()
    try:
        run_result = agent.run(prompt)
    except ShellAbort as e:
        return {
            "calls": [name for name, _ in calls],
            "shell_calls": shell_calls,
            "aborted": str(e),
            "output": None,
            "state": "aborted",
            "success": False,
            "error": str(e),
            "elapsed": round(time.time() - started, 2),
        }

    return {
        "calls": [name for name, _ in calls],
        "shell_calls": shell_calls,
        "aborted": None,
        "output": getattr(run_result, "output", None),
        "state": getattr(run_result, "state", None),
        "success": getattr(run_result, "success", False),
        "error": getattr(run_result, "error", None),
        "elapsed": round(time.time() - started, 2),
    }


def test_agent_chooses_find_file_not_shell(runs=None):
    """Finding a lost file by name MUST use find_file, never PowerShell or disk_tree."""
    model = os.getenv("FIND_FILE_TEST_MODEL", "gemini-3.1-flash-lite")
    provider = os.getenv("FIND_FILE_TEST_PROVIDER", "google")
    if runs is None:
        runs = int(os.getenv("FIND_FILE_TEST_RUNS", "3"))

    failures = []
    for i in range(1, runs + 1):
        shot = _run_main_live_once(
            f"Найди файл {TARGET} на диске, я его потерял",
            model=model, provider=provider,
        )
        print(f"\n[find_file run {i}/{runs}] tools={shot['calls']} elapsed={shot['elapsed']}s")
        if shot["shell_calls"]:
            failures.append(f"run {i}: ABORTED - shell {shot['shell_calls']} ({shot['aborted']})")
        elif "find_file" not in shot["calls"]:
            failures.append(f"run {i}: find_file never called; tools={shot['calls']}")
        elif shot["aborted"]:
            failures.append(f"run {i}: aborted - {shot['aborted']}")
        else:
            print(f"[find_file run {i}/{runs}] OK -> {shot['output']}")

    assert not failures, "\n".join(failures)


def test_agent_chooses_disk_tree_not_shell(runs=None):
    """Finding the largest folder by size MUST use disk_tree, never PowerShell."""
    model = os.getenv("FIND_FILE_TEST_MODEL", "gemini-3.1-flash-lite")
    provider = os.getenv("FIND_FILE_TEST_PROVIDER", "google")
    if runs is None:
        runs = int(os.getenv("FIND_FILE_TEST_RUNS", "3"))

    failures = []
    for i in range(1, runs + 1):
        shot = _run_main_live_once(
            "Какая папка на диске самая большая по размеру? Назови её.",
            model=model, provider=provider,
        )
        print(f"\n[disk_tree run {i}/{runs}] tools={shot['calls']} elapsed={shot['elapsed']}s")
        if shot["shell_calls"]:
            failures.append(f"run {i}: ABORTED - shell {shot['shell_calls']} ({shot['aborted']})")
        elif "disk_tree" not in shot["calls"]:
            failures.append(f"run {i}: disk_tree never called; tools={shot['calls']}")
        elif shot["aborted"]:
            failures.append(f"run {i}: aborted - {shot['aborted']}")
        else:
            print(f"[disk_tree run {i}/{runs}] OK -> {shot['output']}")

    assert not failures, "\n".join(failures)


if __name__ == "__main__":
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else None

    print("=== deterministic: code agent lists find_file as lazy ===")
    test_code_agent_lists_find_file_as_lazy()
    print("PASS")

    print("\n=== deterministic: main agent lists find_file as lazy ===")
    test_main_agent_lists_find_file_as_lazy()
    print("PASS")

    print("\n=== deterministic: main agent lists disk_tree as lazy ===")
    test_main_agent_lists_disk_tree_as_lazy()
    print("PASS")

    print("\n=== deterministic: index age + self-heal ===")
    test_index_reports_age_and_self_heals()
    print("PASS")

    print("\n=== deterministic: find_file resolves target ===")
    test_find_file_resolves_target()
    print("PASS")

    print("\n=== live: main agent must choose find_file (shell aborts) ===")
    test_agent_chooses_find_file_not_shell(runs=runs)

    print("\n=== live: main agent must choose disk_tree (shell aborts) ===")
    test_agent_chooses_disk_tree_not_shell(runs=runs)

    print("\nALL TESTS PASSED")
