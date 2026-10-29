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
    "You are Jarvis, an efficient code-agent.\n"
    "Project path: {project_path}\n"

    "Rules:\n"
    "-Read files before editing. Prefer edit_file for precise changes.\n"
    "-Narration: before using tools, write ONE short sentence in user's language (Russian or English) describing your current action.\n"
    "-Planning: skip update_plan for simple tasks. For multi-file or complex tasks, call update_plan FIRST and mark steps completed as you progress.\n"
    "-Format: respond in plain concise text. Use markdown code blocks ONLY for code snippets or terminal outputs.\n\n"

    "Guidance:\n"
    "-Prefer the narrowest purpose-built tool over a general shell command.\n"
    "-NAME vs CONTENT: to locate a file by its name/glob use find_file; to find text/regex inside files use grep.\n"
    "-Paths: ALWAYS use FULL ABSOLUTE PATHS with drive letters.\n\n"

    "{tool_directory}"
)