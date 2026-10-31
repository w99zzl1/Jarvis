import os
from abc import ABC, abstractmethod
from typing import Dict, Any

from shared.scan_cache import LAST_SCAN_REGISTRY


def resolve_path(project_path: str, path: str, *, allow_scan_cache: bool = True) -> str:
    """Resolve a tool path against the project root.

    Absolute paths are used as-is so the agent can reach any file on the
    machine; relative paths are resolved against ``project_path``.

    When that does not exist, and ``allow_scan_cache`` is set, the last disk
    scan gets a say: a bare ``Videos`` or ``Downloads`` resolves to the real
    absolute path it reported, so "delete that folder you found" works without
    the model having to copy a long path. An ambiguous or unknown reference
    falls through untouched - resolving something we guessed at could point a
    delete at the wrong folder.
    """
    if not path or path == ".":
        return os.path.abspath(project_path)
    if os.path.isabs(path):
        return os.path.normpath(path)

    direct = os.path.normpath(os.path.join(project_path, path))
    if os.path.exists(direct) or not allow_scan_cache:
        return direct

    try:
        hit = LAST_SCAN_REGISTRY.resolve(path)
    except Exception:
        # The cache is a convenience; never fail a tool call over it.
        return direct
    return hit or direct


def resolve_note(path: str) -> str:
    """Explain a cache-based resolution for the tool output, if one happened."""
    try:
        res = LAST_SCAN_REGISTRY.lookup(path)
    except Exception:
        return ""
    return res.explain() if res.ok and res.kind != _EXACT else ""


_EXACT = "exact_path"


def python_syntax_error(path: str) -> str | None:
    """Return a syntax error message for a Python file, or None if it parses.

    The code agent used to report a successful edit even when the result no
    longer parsed: an edit could swallow a `def` line, or escaped quotes could
    land in the file verbatim. Checking here turns a silently broken project
    into feedback the model can act on immediately.
    """
    if not path.lower().endswith((".py", ".pyw")):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
    except OSError:
        return None
    try:
        compile(source, path, "exec")
    except SyntaxError as exc:
        return f"{exc.msg} (line {exc.lineno}, offset {exc.offset})"
    return None


NARRATE_PROPERTY = {
    "type": "string",
    "description": (
        "Short phrase in English, first person, describing what you are doing "
        "right now, e.g. 'reading notes notes.txt'. Shown live in the chat. "
        "Keep it under 60 characters."
    ),
}


class Tool(ABC):
    name: str
    description: str
    parameters: Dict[str, Any]

    @abstractmethod
    def execute(self, *args, **kwargs) -> Dict[str, Any]: ...

    def get_schema(self) -> Dict[str, Any]:
        params = dict(self.parameters)
        # Google provider rejects "additionalProperties" in function declarations
        params.pop("additionalProperties", None)
        props = dict(params.get("properties") or {})
        props.setdefault("narrate", NARRATE_PROPERTY)
        params["properties"] = props
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": params
            }
        }