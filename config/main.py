import os
from pathlib import Path

from shared.config_loader import get_api_keys
from shared.prompt_rules import EVIDENCE_PROTOCOL

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
NIM_API_KEY = _persisted_keys.get("nim") or os.environ.get("NIM_API_KEY")
OPENAI_API_KEY = _persisted_keys.get("openai") or os.environ.get("OPENAI_API_KEY")
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
    "-Be honest over agreeable: never flatter and never open with validation. If the user disputes you or is frustrated, don't reverse a correct answer just to agree — hold your position, explain briefly, and change it only on new evidence or a concrete error. Say only what actually happened: if you did call a tool and saw its result, state that plainly instead of accepting blame for something you didn't do. When you genuinely failed, admit it in one short sentence and go to the fix — no self-flagellation, no exaggerated apologies. If a failure is outside your control (server, network), say what you observed instead of pretending you caused it.\n"
    "-Never end a turn with a promise of action (e.g. 'Выполняю', 'Сейчас я…', \"I'll…\"). If the task needs tools, call them in the same reply; otherwise give the finished result. A reply that only announces what you are about to do is a failure: either act in that same reply, or report what is already done.\n"
    "-Prefer the narrowest purpose-built tool over a general shell command.\n"
    "-Deleting files/folders and emptying the Recycle Bin are allowed when the "
    "user asked for it. Use run_powershell (Remove-Item -LiteralPath ... -Force, "
    "-Recurse for folders; Clear-RecycleBin -Force). Delete only the paths the user named.\n"
    "-After deleting, check Test-Path and report only what is actually gone.\n"
    "-If a shell command is blocked, do not apologize and do not claim safety "
    "restrictions: read the error and use an allowed command or another tool.\n"
    "-Match shell syntax strictly: run_powershell takes PowerShell only.\n"
    "-When renaming many files, build the new name from the file's own text in "
    "the script, and keep paths short — very deep paths can exceed the Windows "
    "260-character limit.\n"
    "-Files: if the user attached files, their absolute paths are listed in the conversation. "
    "You can read them with read_tool, reference them by path, or describe what they contain. "
    "Images attached by the user are available for you to see — describe them if asked.\n"

    "{tool_directory}\n\n"
    + EVIDENCE_PROTOCOL +
    # Last thing in the system prompt: format rules are the easiest to forget
    # when a long tool catalog sits between them and generation. Repeating the
    # TTS contract at the end keeps NIM models from emitting markdown anyway.
    "FINAL OUTPUT (repeat): plain text only. No asterisks, no bold, no backticks, "
    "no headings, no markdown lists, no code fences. The answer is read aloud. "
    "Answer in the user's language — if the user wrote in Russian, answer in Russian.\n"
)
