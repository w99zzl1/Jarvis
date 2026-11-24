import os
from dataclasses import dataclass, field
from typing import List, Optional

from shared.prompt_rules import EVIDENCE_PROTOCOL


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
    "WORKING CONTRACT:\n"
    "- Finish the task in this reply. A user request is already the confirmation; "
    "never end with a promise or ask 'shall I proceed?'. If work remains, call a "
    "tool; a text-only reply must contain the finished result.\n"
    "- Use narrow tools for what they are for: read_file/edit_file/write_file for "
    "files, grep/find_file for content and names, disk_tree for layout, shell only "
    "to run programs. The shell is non-interactive and reports real exit codes.\n"
    "- Plan complex work with update_plan and keep the plan current as you go.\n"
    "- After changing code, run the relevant syntax check or test. Claim success "
    "only for what you actually ran and observed; narration is not completion.\n"
    "- On failure, read the reported stdout/stderr/exit code, fix the cause and "
    "retry. Do not describe a fix you did not apply.\n"
    "- Never `git add -A` or commit without an explicit user request.\n"
    "- The project path is a hint, not a sandbox: use absolute paths outside it "
    "when the task genuinely needs them.\n"
    "- Be accurate, not agreeable. Hold a correct position when challenged, change "
    "it only with new evidence. Acknowledge real mistakes briefly and move to the fix.\n"
    "\n"
    "OUTPUT: concise text in the user's language; markdown only for code or "
    "terminal output. Narrate one short sentence before tool calls.\n"
    "\n"
    "{tool_directory}\n\n"
    + EVIDENCE_PROTOCOL
)