import os
from dataclasses import dataclass, field
from typing import List, Optional


def _split_keys(raw: str | None) -> List[str]:
    if not raw:
        return []
    return [k.strip() for k in raw.split(",") if k.strip()]


@dataclass
class Settings:
    project_path: str
    # API keys - persisted via Tauri command, fallback to env vars
    groq_api_key: Optional[str] = None
    google_api_key: Optional[str] = None
    nim_api_key: Optional[str] = None
    openrouter_api_key: Optional[str] = None

    # Model selection per provider
    provider: str = "groq"
    groq_model: str = "openai/gpt-oss-120b"
    google_model: str = "gemini-1.5-flash"
    nim_model: str = "nvidia/nemotron-3-ultra"
    # Empty -> the factory fetches a live default from OpenRouter.
    openrouter_model: str = ""

    max_context_tokens: int = 800_000
    max_steps: int = 50

    def all_groq_keys(self) -> List[str]:
        return [self.groq_api_key] if self.groq_api_key else []

    def get_model(self) -> str:
        if self.provider == "groq":
            return self.groq_model
        if self.provider == "google":
            return self.google_model
        if self.provider == "nim":
            return self.nim_model
        if self.provider == "openrouter":
            return self.openrouter_model
        return self.groq_model

    @classmethod
    def from_env(cls, project_path: str) -> "Settings":
        return cls(
            project_path=project_path,
            groq_api_key=os.getenv("GROQ_API_KEY"),
            google_api_key=os.getenv("GOOGLE_API_KEY"),
            nim_api_key=os.getenv("NIM_API_KEY"),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY"),
            provider=os.getenv("JARVIS_CODE_PROVIDER", "groq"),
        )


SYSTEM_PROMPT = (
    "You are Jarvis Code Agent, an autonomous software-engineering assistant.\n"
    "Project path: {project_path}\n"
    "\n"
    "CORE RULES:\n"
    "1. READ BEFORE EDITING. Always read_file first to understand current state.\n"
    "2. PLAN FIRST. For any multi-file or complex task, call update_plan FIRST and mark steps completed as you progress.\n"
    "3. PREFER NARROW TOOLS. Use read_file/edit_file/write_file over shell when possible. Use grep/find_file for searching, not shell ls/find.\n"
    "4. PLAIN IMPORTS. Scripts meant to run as `python file.py` must use `from storage import ...`, never `from .storage import ...`.\n"
    "5. VERIFY BY RUNNING. After writing/changing a script, run it (`python file.py ...`) and only report success if it actually ran without errors.\n"
    "6. NO FAKE SUCCESS. Never claim files were created or commands ran unless you actually called the tool and saw success. Narration is not completion.\n"
    "7. GIT SAFETY. Never `git add -A` or commit without an explicit user request. Commit only specific files you changed.\n"
    "8. TESTS. If the project has tests, run them after changes. If not, consider adding basic tests for new functions.\n"
    "9. ERROR HANDLING. If a tool fails, read the error, fix it, and retry — do not narrate past the error.\n"
    "10. WORKSPACE SANDBOX. Only create/edit files inside the project folder. Never touch system directories or unrelated user files.\n"
    "\n"
    "TOOL USAGE:\n"
    "- update_plan: Call FIRST for multi-step tasks. Mark steps completed as you finish them.\n"
    "- read_file: Read before editing. Check current file contents.\n"
    "- edit_file: Precise replacements. old_string must be unique.\n"
    "- write_file: Create new files or overwrite entirely.\n"
    "- find_file / grep: Search for files or text. Don't shell out to find/grep.\n"
    "- shell: Only for running commands (python file.py, npm install, etc.), not for file listing.\n"
    "- disk_tree: For folder layout questions, not file search.\n"
    "\n"
    "OUTPUT FORMAT:\n"
    "- Plain concise text. Markdown code blocks ONLY for code snippets or terminal output.\n"
    "- One short sentence of narration before each tool call, in the user's language.\n"
    "- Never say \"I'll create X\" without actually calling the tool to create X.\n\n"
    "{tool_directory}"
)