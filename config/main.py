import os
from pathlib import Path

from shared.config_loader import get_api_keys

ROOT = Path(__file__).resolve().parent.parent

# --- API key persistence ---
# Keys live in the Tauri config file (%APPDATA%\com.jarvis.assistant\api_keys.json),
# written by the settings UI, and are read through shared.config_loader. Nothing
# is hardcoded here; this module only exposes the values the rest of the app
# imports, so they can stay as a last-resort fallback behind the loader.
_persisted_keys = get_api_keys()

GROQ_API_KEY = _persisted_keys.get("groq") or os.environ.get("GROQ_API_KEY", "")

# --- Persisted config (saved in app config dir, fallback to env vars) ---
# These are loaded by the Tauri commands; keep env vars as fallback defaults.
PRIMARY_API_KEY = os.environ.get("JARVIS_PRIMARY_API_KEY", "")
PROVIDER = os.environ.get("JARVIS_PROVIDER", "groq")
GROQ_MODEL = os.environ.get("JARVIS_GROQ_MODEL", "openai/gpt-oss-120b")
GOOGLE_MODEL = os.environ.get("JARVIS_GOOGLE_MODEL", "gemini-1.5-flash")
NIM_MODEL = os.environ.get("JARVIS_NIM_MODEL", "nvidia/nemotron-3-ultra")
VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

LLM_BASE_URL = os.environ.get("JARVIS_LLM_BASE_URL", "")
# Primary API key fallback: if not set via Tauri persistence, fall back to GROQ_API_KEY
LLM_API_KEY = os.environ.get("JARVIS_LLM_API_KEY", GROQ_API_KEY or PRIMARY_API_KEY or "")
GOOGLE_API_KEY = _persisted_keys.get("google") or os.environ.get("GOOGLE_API_KEY")
OPENROUTER_API_KEY = _persisted_keys.get("openrouter") or os.environ.get("OPENROUTER_API_KEY")
CONTEXT_MAX_TOKENS = int(os.environ.get("JARVIS_CONTEXT_TOKENS", "30000"))

SYSTEM_PROMPT = (
    "You are JARVIS, an autonomous voice assistant. "
    "Task: execute user commands autonomously.\n\n"

    "CRITICAL TTS FORMAT:\n"
    "Answer in the user's language, but keep it SHORT, conversational, direct. "
    "NO markdown whatsoever — no asterisks, bold, lists, code blocks, backticks, headings, or any formatting characters. "
    "Plain text only, because the answer is read aloud by TTS. Do not ramble, do not add fluff, do not explain unless asked.\n\n"

    "Rules:\n"
    "-Perform tasks autonomously without asking user questions.\n"
    "-Prefer the narrowest purpose-built tool over a general shell command.\n"
    "-Match shell syntax strictly: run_powershell takes PowerShell only, run_cmd takes cmd.exe only.\n"
    "-Files: if the user attached files, their absolute paths are listed in the conversation. "
    "You can read them with read_tool, reference them by path, or describe what they contain. "
    "Images attached by the user are available for you to see — describe them if asked.\n"

    "{tool_directory}"
)
