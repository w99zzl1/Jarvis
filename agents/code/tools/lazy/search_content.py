import os
import re
from typing import Dict, Any, List
from ..base import Tool, resolve_path

# Generated and vendored trees. Walking src-tauri/target or node_modules made
# a plain grep take minutes and returned matches inside compiled binaries.
_SKIP_DIRS = {
    "__pycache__", ".git", ".hg", ".svn", ".venv", "venv", "env",
    "node_modules", "target", "dist", "build", ".next", ".nuxt",
    ".cache", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "site-packages", ".idea", ".vscode", "coverage", ".gradle",
}

MAX_FILE_BYTES = 2 * 1024 * 1024


class SearchContentTool(Tool):
    name = "grep"
    description = (
        "grep/regex CONTENT search INSIDE files (ripgrep-like). "
        "Use this to find text, code, functions, strings WITHIN files in the project. "
        "NOT for locating a file by its filename - for that use find_file."
    )
    catalog_hint = (
        "search TEXT/REGEX INSIDE file contents (ripgrep-like) - for strings, "
        "function names, code. Not for finding a file by its name"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Text or regex pattern to search for",
            },
            "path": {
                "type": "string",
                "description": "Directory to search (relative to project root). Default is the whole project",
                "default": ".",
            },
            "file_pattern": {
                "type": "string",
                "description": "File extension filter (e.g. '*.py', '*.js', '*.ts', '*.txt')",
                "default": "*",
            },
            "case_sensitive": {"type": "boolean", "default": False},
            "use_regex": {"type": "boolean", "default": False},
            "limit": {
                "type": "integer",
                "description": "Maximum number of matches",
                "default": 30,
            },
        },
        "required": ["query"],
    }

    def __init__(self, project_path: str):
        self.project_path = project_path

    def execute(
        self,
        query: str,
        path: str = ".",
        file_pattern: str = "*",
        case_sensitive: bool = False,
        use_regex: bool = False,
        limit: int = 30,
    ) -> Dict[str, Any]:
        scope = resolve_path(self.project_path, path)
        if not os.path.isdir(scope):
            return {"success": False, "error": f"Directory not found: {scope}"}

        limit = max(1, min(int(limit), 500))
        ext = None
        if file_pattern and file_pattern != "*":
            ext = file_pattern.lower().lstrip("*").lstrip(".")

        flags = 0 if case_sensitive else re.IGNORECASE
        compiled = None
        if use_regex:
            try:
                compiled = re.compile(query, flags)
            except re.error:
                return {"success": False, "error": f"Invalid regex: {query}"}

        results: List[Dict] = []
        matches_count = 0

        for root, dirs, files in os.walk(scope):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for fname in files:
                full = os.path.join(root, fname)
                if ext and not full.lower().endswith("." + ext):
                    continue
                if file_pattern != "*" and not full.lower().endswith(file_pattern.lower().lstrip("*").lstrip(".")):
                    continue
                # Build output and binaries are never the answer and used to
                # dominate the runtime of an unmatched search.
                try:
                    if os.path.getsize(full) > MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                try:
                    with open(full, "r", encoding="utf-8", errors="ignore") as f:
                        for line_no, line in enumerate(f, 1):
                            line = line.rstrip("\n\r")
                            if compiled:
                                if compiled.search(line):
                                    results.append({
                                        "file": full,
                                        "line": line_no,
                                        "content": line.strip(),
                                    })
                                    matches_count += 1
                            else:
                                search_text = line if case_sensitive else line.lower()
                                if (query if case_sensitive else query.lower()) in search_text:
                                    results.append({
                                        "file": full,
                                        "line": line_no,
                                        "content": line.strip(),
                                    })
                                    matches_count += 1
                            if matches_count >= limit:
                                return {
                                    "success": True,
                                    "query": query,
                                    "matches_count": matches_count,
                                    "matches": results,
                                    "truncated": True,
                                }
                except (PermissionError, OSError):
                    continue

        return {
            "success": True,
            "query": query,
            "matches_count": matches_count,
            "matches": results,
            "truncated": False,
        }
