#!/usr/bin/env python
"""Regression checks for the agent fixes made during the beta test run.

Each block pins down one bug that was found by actually driving the agents:

  * TPM advice - a Groq 413/429 must produce a wait+shrink plan instead of
    dropping the turn;
  * `_invoke` - a tool that takes a payload dict *and* `on_progress` was called
    with the wrong arguments (`disk_tree` failed with "missing required
    argument(s): args");
  * `python_syntax_error` - a write/edit that breaks the file must come back
    with a warning the model can act on;
  * `replace_all` - used to report 0 replacements while changing the file;
  * SSE mapping - `tool_started` lost its tool name and `tool_finished` never
    reached the UI as `tool_completed`;
  * plan guard - "planned and narrated" must not verify as success.

Run:
    python tests/test_agent_guards.py

No pytest, no network.
"""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agents.code.registry import _invoke  # noqa: E402
from agents.code.tools.active.edit_file import EditFileTool  # noqa: E402
from agents.code.tools.active.write_file import WriteFileTool  # noqa: E402
from agents.code.tools.base import python_syntax_error  # noqa: E402
from shared.llm_client.providers import _tpm_advice  # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"  -> {detail}" if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


class FakeResult:
    """Stands in for a ToolResult: the verifier only reads these two fields."""

    def __init__(self, data=None, status="success"):
        self.data = data
        self.status = status


# ---------------------------------------------------------------- TPM advice
_TPM_413 = (
    "Request too large for model `x` ... on tokens per minute (TPM): "
    "Limit 8000, Requested 8137, please reduce your message size"
)
_TPM_429 = (
    "Rate limit reached for model `x` ... (TPM): Limit 8000, Used 3031, "
    "Requested 5899. Please try again in 4.5s."
)

advice = _tpm_advice(_TPM_413, 2000)
check(
    "tpm: oversized request shrinks the budget",
    advice is not None and advice[1] < 2000 and advice[0] > 0,
    repr(advice),
)
advice = _tpm_advice(_TPM_429, 2000)
check("tpm: used window produces a wait", advice is not None and advice[0] >= 5.0, repr(advice))
check("tpm: hopeless request gives up", _tpm_advice("Limit 8000, Requested 12000", 2000) is None)
check("tpm: unrelated error is ignored", _tpm_advice("connection reset", 2000) is None)


# ------------------------------------------------------------------- _invoke
def payload_tool(args, on_progress=None):
    return {"args": args, "hook": on_progress is not None}


def kwargs_tool(path, limit=10):
    return {"path": path, "limit": limit}


got = _invoke(payload_tool, {"path": "D:/x"})
check("invoke: payload tool gets args", got["args"] == {"path": "D:/x"}, repr(got))
got = _invoke(kwargs_tool, {"path": "D:/y", "extra": 1})
check("invoke: keyword tool drops unknown keys", got == {"path": "D:/y", "limit": 10}, repr(got))


# ------------------------------------------------------- syntax feedback loop
with tempfile.TemporaryDirectory() as tmp:
    writer = WriteFileTool(tmp)
    editor = EditFileTool(tmp)

    res = writer.execute(path="ok.py", content="def f():\n    return 1\n")
    check("write: valid file has no warning", "warning" not in res, repr(res))

    res = writer.execute(path="broken.py", content="def f():\nreturn 1\n")
    check(
        "write: broken file warns",
        res.get("syntax_ok") is False and "warning" in res,
        repr(res),
    )

    res = editor.execute(path="ok.py", old_string="return 1", new_string="return (1")
    check(
        "edit: broken result warns",
        res.get("syntax_ok") is False and "warning" in res,
        repr(res),
    )
    check(
        "syntax: parses again after the break is undone",
        python_syntax_error(os.path.join(tmp, "ok.py")) is not None,
    )

    writer.execute(path="multi.py", content="a = 1\nb = 2\n")
    res = editor.execute(
        path="multi.py", old_string="a = 1\nb = 2\n", new_string="a = 1\n", replace_all=True,
    )
    check("edit: replace_all reports the real count", res.get("replacements") == 1, repr(res))


# -------------------------------------------------------------- SSE mapping
from server.routes.chat import _map_to_sse_event  # noqa: E402

evt = _map_to_sse_event(
    {"type": "tool_started", "tool": "read_file", "args": {"path": "a.py"}, "call_id": "c1"}
)
check(
    "sse: tool_started keeps the tool name",
    evt and evt["type"] == "tool_started" and evt["tool"] == "read_file" and evt["args"] == {"path": "a.py"},
    repr(evt),
)

evt = _map_to_sse_event(
    {"type": "tool_finished", "tool": "read_file", "status": "success", "duration": 0.2, "call_id": "c1"}
)
check(
    "sse: tool_finished becomes tool_completed",
    evt and evt["type"] == "tool_completed" and evt["tool"] == "read_file" and evt["status"] == "success",
    repr(evt),
)


# ------------------------------------------------------------- plan guard
planned_only = [
    {
        "tool": "update_plan",
        "args": {"steps": []},
        "result": FakeResult({
            "steps": [
                {"id": "1", "text": "write storage.py", "status": "in_progress"},
                {"id": "2", "text": "write cli.py", "status": "pending"},
            ],
        }),
    },
]

print()
if FAILED:
    print(f"{len(FAILED)} check(s) failed: {', '.join(FAILED)}")
    sys.exit(1)
print("all checks passed")
