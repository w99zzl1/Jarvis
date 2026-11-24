"""Content search for the main agent.

Claude Code exposes Glob and Grep to the same agent that edits files. The main
voice agent previously had only filename search, so code questions forced it
into shell `Select-String` one-liners. This is a thin main-registry adapter over
the code agent's proven content search.
"""

from agents.code.tools.lazy.search_content import SearchContentTool

from .registry import ToolResult


class GrepTool(SearchContentTool):
    name = "grep"
    category = "system"
    cost = "low"
    destructive = False
    group = "on_demand"

    def to_openai_spec(self) -> dict:
        # get_schema() already adds the `narrate` property the main registry
        # expects, so the main and code tool declarations stay identical.
        return self.get_schema()

    def reset(self) -> None:
        return None

    def execute(self, args: dict, **_kwargs) -> ToolResult:
        data = super().execute(**(args or {}))
        if not data.get("success"):
            return ToolResult(status="error", error=str(data.get("error", "grep failed")))
        return ToolResult(status="success", data=data)
