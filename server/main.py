import asyncio
import logging
import sys
import os
import time
from pathlib import Path
from typing import Any, Optional, Dict, List

_root = str(Path(__file__).resolve().parent.parent)
_server_dir = str(Path(__file__).resolve().parent)
if _root not in sys.path:
    sys.path.insert(0, _root)
if _server_dir not in sys.path:
    sys.path.insert(0, _server_dir)

from fastapi import FastAPI, UploadFile, File, Request, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from routes.chat import chat_route, chat_stream
from routes.agent import agent_route, agent_chat_stream
from routes.code_agent import code_chat_route, code_chat_stream
from routes.stt import transcribe_audio, stt_stream
from shared.llm_client.model_fetcher import check_connected_providers_and_fetch_models, fetch_provider_models
from shared.config_loader import get_selected_models
from shared import paths as _paths
from security import install as install_security
from security import rotate_token, validate_token_rotation

log = logging.getLogger("server")

app = FastAPI(title="Jarvis Agent API")

# Strong reference to the startup warm-up task (see startup_event).
_warmup_task: "asyncio.Task | None" = None

@app.on_event("startup")
async def startup_event():
    # Bind the socket immediately. Model discovery and the whole-system file
    # index can take many seconds (network + a full disk walk); doing them here
    # made the UI hit ERR_CONNECTION_REFUSED on every launch because the port
    # was not accepting yet. They now warm up in a worker thread.
    global _warmup_task
    # Hold a strong reference: the event loop only keeps weak references to
    # tasks, so without this the warm-up could be collected mid-run.
    _warmup_task = asyncio.create_task(asyncio.to_thread(_warm_up_backend))


def _warm_up_backend() -> None:
    log.info("[SERVER] Checking connected providers and fetching supported models via official API...")
    try:
        models_map = check_connected_providers_and_fetch_models()
        log.info(f"[SERVER] Startup model check completed: {models_map}")
    except Exception as e:
        log.warning(f"[SERVER] Startup model check error: {e}")

    # Kick off the whole-system filename index in the background so the first
    # lost-file search reads a ready index instead of blocking a request.
    try:
        from shared.file_index import ensure_built
        ensure_built()
    except Exception as e:
        log.warning(f"[SERVER] File index startup error: {e}")

class FetchModelsRequest(BaseModel):
    api_keys: Optional[Any] = None


@app.get("/health")
async def health():
    """Liveness probe used by the Tauri shell to report backend problems.

    Deliberately trivial: it must answer even when no API key is configured,
    because the UI needs to distinguish "backend did not start" from "model
    provider is not reachable".
    """
    return {
        "status": "ok",
        "service": "jarvis-backend",
        "data_dir": str(_paths.app_dir()),
        "workspace": str(_paths.workspace_dir() or ""),
    }


@app.post("/models/fetch")
@app.get("/models/live")
async def get_live_models(req: Optional[FetchModelsRequest] = None):
    api_keys_dict = req.api_keys if req and req.api_keys else None
    norm_keys = _norm_api_keys(api_keys_dict)
    models_map = check_connected_providers_and_fetch_models(norm_keys)
    return {"success": True, "models": models_map}


@app.get("/conversations")
async def list_conversations():
    """Newest-first summaries of every stored conversation (chat-history drawer)."""
    from server.conversations import store as conv_store

    return {"success": True, "conversations": conv_store.list()}


@app.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str):
    """Full event log of one conversation, so the UI can replay it."""
    from server.conversations import store as conv_store

    return {"success": True, "id": conversation_id, "events": conv_store.load(conversation_id)}


@app.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    from server.conversations import store as conv_store

    conv_store.delete(conversation_id)
    return {"success": True}


# ============================================================================
# File uploads (drag-drop / paste attachments from UI)
# ============================================================================

_UPLOAD_DIR = str(_paths.uploads_dir())


def _ensure_upload_dir():
    os.makedirs(_UPLOAD_DIR, exist_ok=True)
    return _UPLOAD_DIR


@app.post("/upload")
async def upload_files(files: List[UploadFile] = File(...)):
    """Save uploaded files to the uploads dir and return file info for the UI."""
    _ensure_upload_dir()
    MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB

    # MIME type whitelist - only allow safe file types
    ALLOWED_MIME_TYPES = frozenset({
        # Images
        "image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp",
        "image/tiff", "image/svg+xml", "image/x-icon", "image/heic", "image/heif",
        # Text / Code
        "text/plain", "text/markdown", "text/csv", "text/html", "text/css",
        "text/javascript", "text/x-python", "text/x-script.python",
        "application/json", "application/xml", "application/yaml",
        "application/x-yaml", "application/toml",
        # Documents
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        # Data
        "application/csv", "application/sql", "application/x-sql",
        # Archives (for inspection only - no execution)
        "application/zip", "application/x-zip-compressed",
        "application/gzip", "application/x-gzip",
        "application/x-tar", "application/x-bzip2", "application/x-xz",
    })

    # Blocked extensions (executable files)
    BLOCKED_EXTENSIONS = frozenset({
        ".exe", ".msi", ".bat", ".cmd", ".ps1", ".psm1", ".psd1",
        ".sh", ".bash", ".zsh", ".fish", ".csh", ".tcsh",
        ".py", ".pyw", ".pyc", ".pyo", ".pyd",
        ".js", ".mjs", ".cjs", ".ts", ".tsx",
        ".jar", ".war", ".ear",
        ".dll", ".so", ".dylib",
        ".app", ".deb", ".rpm", ".apk", ".ipa",
        ".scr", ".com", ".pif", ".application",
        ".gadget", ".msc", ".jar", ".wsf", ".wsh",
        ".ps1xml", ".psc1", ".psc2", ".msh", ".msh1", ".msh2",
        ".mshxml", ".msh1xml", ".msh2xml",
        ".reg", ".inf", ".ini", ".pol",
    })

    saved = []
    for f in files:
        # Sanitize filename
        safe_name = "".join(c for c in f.filename if c.isalnum() or c in "._- ") or "file"
        # Reject path traversal or absolute paths
        if "/" in safe_name or "\\" in safe_name or ".." in safe_name:
            raise ValueError(f"Unsafe filename: {f.filename}")

        # Check file extension
        _, ext = os.path.splitext(safe_name.lower())
        if ext in BLOCKED_EXTENSIONS:
            raise ValueError(f"File type not allowed: {ext}")

        # Check MIME type
        mime = f.content_type or "application/octet-stream"
        if mime not in ALLOWED_MIME_TYPES:
            raise ValueError(f"MIME type not allowed: {mime}")

        dest = os.path.join(_UPLOAD_DIR, safe_name)
        # Avoid collisions
        if os.path.exists(dest):
            base, ext = os.path.splitext(safe_name)
            dest = os.path.join(_UPLOAD_DIR, f"{base}_{int(time.time()*1000)}{ext}")
        content = await f.read()
        if len(content) > MAX_UPLOAD_BYTES:
            raise ValueError(f"File too large: {len(content) / (1024*1024):.1f} MB (max 20 MB)")
        with open(dest, "wb") as out:
            out.write(content)
        saved.append({
            "path": os.path.abspath(dest),
            "name": f.filename,
            "mime": mime,
            "size": len(content),
        })
    return {"success": True, "attachments": saved}


# ============================================================================
# Token Rotation
# ============================================================================

class RotateTokenRequest(BaseModel):
    """Request to rotate the API token (requires current valid token)."""
    pass  # Current token is validated via header


@app.post("/auth/rotate")
async def rotate_token_endpoint(request: Request):
    """Rotate the API token. Requires valid current token in header.

    Returns new token and expiry timestamp.
    """
    # Get current token from header
    current_token = request.headers.get("x-jarvis-token", "").strip()
    if not current_token:
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing current token for rotation."},
        )

    # Validate current token matches stored hash
    if not validate_token_rotation(current_token):
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid current token."},
        )

    # Generate new token
    new_token, expiry = rotate_token()
    return {
        "success": True,
        "token": new_token,
        "expiry": expiry,
        "ttl_seconds": 24 * 60 * 60,
    }


# ============================================================================
# STT/TTS Settings
# ============================================================================

class STTConfigRequest(BaseModel):
    """Request to update STT configuration."""
    model: str | None = None  # tiny, base, small, medium, large
    device: str | None = None  # cpu, cuda
    compute_type: str | None = None  # int8, float16, float32


class TTSConfigRequest(BaseModel):
    """Request to update TTS configuration."""
    provider: str | None = None  # silero (the only supported engine)
    enabled: bool | None = None
    model_id: str | None = None  # v4_ru, v3_en, etc.
    speaker: str | None = None  # aidar, baya, kseniya, xenia, eugene, random
    language: str | None = None  # ru, en
    sample_rate: int | None = None  # 8000, 16000, 48000
    device: str | None = None  # cpu, cuda


@app.get("/settings/stt")
async def get_stt_settings():
    """Get current STT configuration."""
    from shared.config_loader import get_stt_config
    return {"success": True, "config": get_stt_config()}


@app.post("/settings/stt")
async def update_stt_settings(req: STTConfigRequest):
    """Update STT configuration. Changes take effect on next transcription."""
    from shared.config_loader import get_app_config, save_app_config
    cfg = get_app_config()
    stt = cfg.get("stt", {})
    if not isinstance(stt, dict):
        stt = {}
    if req.model is not None:
        stt["model"] = req.model
    if req.device is not None:
        stt["device"] = req.device
    if req.compute_type is not None:
        stt["compute_type"] = req.compute_type
    cfg["stt"] = stt
    save_app_config(cfg)
    # Invalidate cached model to force reload with new config
    try:
        from server.routes.stt import _invalidate_stt_model
        _invalidate_stt_model()
    except Exception:
        pass
    return {"success": True, "config": stt}


@app.get("/settings/tts")
async def get_tts_settings():
    """Get current TTS configuration."""
    from shared.config_loader import get_tts_config
    return {"success": True, "config": get_tts_config()}


@app.post("/settings/tts")
async def update_tts_settings(req: TTSConfigRequest):
    """Update TTS configuration. Changes take effect on next synthesis."""
    from shared.config_loader import get_app_config, save_app_config
    cfg = get_app_config()
    tts = cfg.get("tts", {})
    if not isinstance(tts, dict):
        tts = {}
    if req.provider is not None:
        if req.provider != "silero":
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail="Only the silero TTS provider is supported")
        tts["provider"] = req.provider
    if req.enabled is not None:
        tts["enabled"] = req.enabled
    if req.model_id is not None:
        tts["model_id"] = req.model_id
    if req.speaker is not None:
        tts["speaker"] = req.speaker
    if req.language is not None:
        tts["language"] = req.language
    if req.sample_rate is not None:
        tts["sample_rate"] = req.sample_rate
    if req.device is not None:
        tts["device"] = req.device
    cfg["tts"] = tts
    save_app_config(cfg)
    # Invalidate cached model to force reload with new config
    try:
        from server.routes.chat import _invalidate_tts_model
        _invalidate_tts_model()
    except Exception:
        pass
    return {"success": True, "config": tts}


# ============================================================================
# Speech-to-Text (microphone transcription via faster-whisper, local/offline)
# ============================================================================

@app.post("/stt/transcribe")
async def stt_transcribe(file: UploadFile = File(...)):
    """Accept a WAV/WEBM/OGG blob from the UI microphone, return transcribed text."""
    return await transcribe_audio(file)


@app.websocket("/stt/stream")
async def stt_stream_endpoint(
    websocket: WebSocket,
    session_id: str,
    token: str = "",
):
    """Bidirectional STT stream: PCM in, partial/final text events out."""
    await stt_stream(websocket, session_id, token)


# Per-launch token + CORS. See server/security.py: the API can run shell
# commands, so listening on localhost is not enough on its own.
install_security(app)


class ModelSpec(BaseModel):
    """
    Явная связка модель + провайдер из UI.
    Рекомендуемый формат — UI отправляет primary_spec именно такой структурой.
    """
    provider: str = Field(..., description="One of: groq, google, openai, openrouter, nim, openai_compat")
    model: str = Field(..., description="Model string for this provider, e.g. gemini-2.0-flash")


class ChatRequest(BaseModel):
    """
    Новый контракт UI→Бэкенд.

    Два поддерживаемых способа передать модель+провайдер (первый имеет приоритет):

    ВАРИАНТ 1 — рекомендуемый, явные spec-структуры:
    {
      "message": "...",
      "primary_spec":    { "provider": "google", "model": "gemini-2.0-flash" },
      "api_keys": { "google": {"key":"..."} , "groq": {"key":"..."} }
    }

    ВАРИАНТ 2 — раздельные поля (для обратной совместимости):
    {
      "message": "...",
      "primary_model": "gemini-2.0-flash",
      "primary_provider": "google"
    }

    ВАРИАНТ 3 — старый selected_model dict (совместимость с legacy UI):
    {
      "message": "...",
      "config": { "selected_model": { "provider":"google", "code":"gemini-2.0-flash" } }
    }

    agent_type: "main" → Main Jarvis, "code" → Code Agent.
    The choice comes from the UI and is never guessed from message text.
    """
    message: str
    agent_type: str = "main"
    conversation_id: Optional[str] = None
    # Explicit UI signal: speak the main-agent reply in voice mode. The backend
    # never infers this from message text or from the agent type.
    synthesize_speech: bool = False

    # ---- Spec-�?�'�?�?��'�?�?�� (���?��?�?��'��' #1) ----
    primary_spec: Optional[ModelSpec] = None

    # ---- �������?��>�?�?�<�� ���?�>�? (���?��?�?��'��' #2) ----
    primary_model: str = ""
    primary_provider: Optional[str] = None

    # ---- ���'���?�<�� ��?�?�"��? selected_model (���?��?�?��'��' #3) ----
    config: Optional[Dict[str, Any]] = None

    # ---- API keys & misc ----
    api_keys: Optional[Any] = None
    api_key: Optional[str] = None
    system_prompt: str = ""
    vision_model: Optional[str] = None

    # Code-agent tools the user switched off in the UI. Removed from both the
    # LLM tool schema and the system prompt for this request.
    disabled_tools: Optional[List[str]] = None

    # Files uploaded by the user and persisted by the /upload endpoint.
    # List of {path, name, mime, size} dicts — absolute server-side paths.
    attachments: Optional[List[Dict[str, Any]]] = None


def _norm_api_keys(raw):
    """Приводит api_keys к стабильному формату (dict или list или None)."""
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        if raw.strip().startswith("{") or raw.strip().startswith("["):
            import json
            try:
                return json.loads(raw)
            except Exception:
                pass
        if "," in raw:
            return [k.strip() for k in raw.split(",") if k.strip()]
        return [raw]
    return None


def _resolve_model_and_provider(
    spec: Optional[ModelSpec],
    model_field: str,
    provider_field: Optional[str],
    config: Optional[Dict[str, Any]],
    fallback_model: str,
    fallback_provider: str,
    spec_field: Optional[tuple] = None,
):
    """
    Унифицированный резолвер: (spec) -> (раздельные поля) -> (config.selected_model)
    -> (сохранённый выбор) -> fallback. Возвращает (model, provider).
    """
    # 1) spec
    if spec is not None:
        return spec.model, spec.provider

    # 2) раздельные поля
    if provider_field and model_field:
        return model_field, provider_field
    if model_field and not provider_field:
        raise ValueError(
            f"Model '{model_field}' given but no 'provider' specified. "
            f"Please set provider explicitly (groq/google/openai/openrouter/nim)."
        )

    # 3) legacy config.selected_model
    if config and isinstance(config, dict):
        sm = config.get("selected_model")
        if isinstance(sm, dict):
            p = sm.get("provider")
            m = sm.get("code") or sm.get("model")
            if p and m:
                return m, p
            if m and not p:
                raise ValueError(
                    f"config.selected_model.code='{m}' set, but selected_model.provider missing."
                )
        # И ещё selected_provider
        sp = config.get("selected_provider")
        if sp:
            if model_field:
                return model_field, sp

    # 4) сохранённый в UI выбор модели, чтобы не подбирать её заново каждый запуск
    if spec_field:
        return spec_field[1], spec_field[0]

    # 5) fallback — только если пользователь ничего не задал про модель вообще
    return fallback_model, fallback_provider


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return {"message": "Jarvis API is running", "version": "3.0 (explicit providers)"}


def _get_default_provider_and_model(api_keys=None):
    import config.main as config_main
    
    has_google = False
    if api_keys and isinstance(api_keys, dict) and api_keys.get("google"):
        has_google = True
    elif config_main.GOOGLE_API_KEY or os.getenv("GOOGLE_API_KEY"):
        has_google = True

    has_groq = False
    if api_keys and isinstance(api_keys, dict) and api_keys.get("groq"):
        has_groq = True
    elif config_main.GROQ_API_KEY or os.getenv("GROQ_API_KEY"):
        has_groq = True

    has_nim = False
    if api_keys and isinstance(api_keys, dict) and api_keys.get("nim"):
        has_nim = True
    elif getattr(config_main, "NIM_API_KEY", None) or os.getenv("NIM_API_KEY"):
        has_nim = True

    # Prefer whatever the user actually connected; Groq stays the last resort.
    if not has_groq:
        if has_google:
            return "", "google"
        if has_nim:
            return "", "nim"
    return "", "groq"


def _resolve_for_request(req: ChatRequest):
    """Возвращает готовые к передаче в фабрику значения (primary model, primary provider)."""
    def_m, def_p = _get_default_provider_and_model(req.api_keys)
    try:
        saved = get_selected_models()
    except Exception:
        saved = None
    pm, pp = _resolve_model_and_provider(
        spec=req.primary_spec,
        model_field=req.primary_model,
        provider_field=req.primary_provider,
        config=req.config,
        fallback_model=def_m,
        fallback_provider=def_p,
        spec_field=(saved["provider"], saved["model"]) if saved else None,
    )
    return pm, pp


# Agent routing is explicit: the UI sends `agent_type`. The backend never
# switches agents based on the message text.


def resolve_stream_models(
    primary_spec=None,
    primary_model: str = "",
    primary_provider: Optional[str] = None,
    api_keys=None,
):
    """Для стрим-роутов, которые FastAPI отдаёт как обычные функции.

    UI шлёт primary_spec, а эти роуты принимали только
    primary_model/primary_provider, поэтому выбор модели молча терялся.
    """
    def _as_spec(raw):
        if raw is None:
            return None
        if isinstance(raw, ModelSpec):
            return raw
        if isinstance(raw, dict):
            provider = raw.get("provider")
            model = raw.get("model") or raw.get("code")
            if provider and model:
                return ModelSpec(provider=provider, model=model)
        return None

    req = ChatRequest(
        message="",
        primary_spec=_as_spec(primary_spec),
        primary_model=primary_model or "",
        primary_provider=primary_provider,
        api_keys=api_keys,
    )
    return _resolve_for_request(req)


# === Main Jarvis / Code Agent (универсальный эндпоинт) ===

@app.post("/chat")
async def chat(req: ChatRequest):
    pm, pp = _resolve_for_request(req)
    loop = asyncio.get_event_loop()
    if req.agent_type == "code":
        result = await loop.run_in_executor(
            None,
            code_chat_route,
            req.message,
            _norm_api_keys(req.api_keys),
            req.api_key,
            req.system_prompt,
            pm, pp,
            req.config,
            req.disabled_tools,
            req.attachments,
            req.conversation_id,
        )
    else:
        result = await loop.run_in_executor(
            None,
            chat_route,
            req.message,
            _norm_api_keys(req.api_keys),
            req.api_key,
            req.system_prompt,
            pm, pp,
            req.vision_model,
            req.config,
            req.conversation_id,
            req.attachments,
            req.synthesize_speech,
        )
    return result


@app.post("/chat/stream")
async def chat_stream_endpoint(req: ChatRequest):
    pm, pp = _resolve_for_request(req)
    if req.agent_type == "code":
        return await code_chat_stream(
            req.message,
            _norm_api_keys(req.api_keys),
            req.api_key,
            req.system_prompt,
            pm, pp,
            req.config,
            conversation_id=req.conversation_id,
            disabled_tools=req.disabled_tools,
            attachments=req.attachments,
        )
    return await chat_stream(
        req.message,
        _norm_api_keys(req.api_keys),
        req.api_key,
        req.system_prompt,
        pm, pp,
        req.vision_model,
        req.config,
        conversation_id=req.conversation_id,
        attachments=req.attachments,
        synthesize_speech=req.synthesize_speech,
    )


# === Agent (legacy) ===

@app.post("/agent/chat")
async def agent_chat(req: ChatRequest):
    pm, pp = _resolve_for_request(req)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        agent_route,
        req.message,
        _norm_api_keys(req.api_keys),
        req.api_key,
        req.system_prompt,
        pm, pp,
        req.config,
    )
    return result


@app.post("/agent/chat/stream")
async def agent_chat_stream_endpoint(req: ChatRequest):
    pm, pp = _resolve_for_request(req)
    return await agent_chat_stream(
        req.message,
        _norm_api_keys(req.api_keys),
        req.api_key,
        req.system_prompt,
        pm, pp,
        req.config,
    )


# === Code Agent ===

@app.get("/code/tools")
async def code_tools_endpoint():
    from routes.code_agent import list_code_tools
    return {"success": True, "tools": list_code_tools()}


@app.post("/code/chat")
async def code_chat(req: ChatRequest):
    pm, pp = _resolve_for_request(req)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        code_chat_route,
        req.message,
        _norm_api_keys(req.api_keys),
        req.api_key,
        req.system_prompt,
        pm, pp,
        req.config,
        req.disabled_tools,
        req.attachments,
    )
    return result


class StopAgentRequest(BaseModel):
    conversation_id: str


@app.post("/agent/stop")
async def agent_stop(req: StopAgentRequest):
    """Cancel the running turn for this conversation.

    Previously a no-op that returned `stopped: False`, so Stop only abandoned
    the SSE stream while the agent thread kept calling tools and spending quota.
    Now it flips the turn's `CancelToken`; the agent loop unwinds at its next
    checkpoint and running shell tools kill their child process.
    """
    from shared.cancellation import registry as cancel_registry
    from shared import agent_job

    stopped = cancel_registry.cancel(req.conversation_id)
    # Closing the job kills the whole process tree, not just the shell the
    # agent happened to be waiting on. Returns the real number of descendants.
    res = agent_job.stop_job(req.conversation_id)
    if stopped:
        log.info("[SERVER] Stop requested for conversation %s", req.conversation_id)
    return {"ok": True, "stopped": stopped, "killed": res.get("killed", 0)}


@app.post("/code/chat/stream")
async def code_chat_stream_endpoint(req: ChatRequest):
    pm, pp = _resolve_for_request(req)
    return await code_chat_stream(
        req.message,
        _norm_api_keys(req.api_keys),
        req.api_key,
        req.system_prompt,
        pm, pp,
        req.config,
        conversation_id=req.conversation_id,
        disabled_tools=req.disabled_tools,
        attachments=req.attachments,
    )


def _die_with_parent() -> None:
    """Exit as soon as the process that spawned us is gone.

    Ctrl+C (or closing the console) kills the Tauri shell without unwinding
    Rust destructors, so this child would otherwise keep port 8765 busy
    forever. Waiting on the parent process handle is the one reliable signal
    available on Windows.
    """
    if os.name != "nt":
        return

    import ctypes
    import threading

    synchronize = 0x00100000
    wait_object_0 = 0x00000000

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32

    handle = kernel32.OpenProcess(synchronize, False, os.getppid())
    if not handle:
        return

    def wait() -> None:
        while True:
            if kernel32.WaitForSingleObject(handle, 1000) == wait_object_0:
                os._exit(0)

    threading.Thread(target=wait, name="parent-watchdog", daemon=True).start()


if __name__ == "__main__":
    import uvicorn
    _die_with_parent()
    uvicorn.run(app, host="127.0.0.1", port=8765)
