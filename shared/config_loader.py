import json
import logging

from shared import paths

log = logging.getLogger("config_loader")


def get_config_dir():
    # Единый каталог данных: %APPDATA%\com.jarvis.assistant (см. shared/paths.py).
    return paths.app_dir()


def get_api_keys():
    p = paths.api_keys_file()
    if p.exists():
        try:
            data = json.loads(p.read_text())
            # Возвращаем только api_keys, как в файле
            return data.get("api_keys", {})
        except Exception as e:
            log.error(f"Failed to load API keys: {e}")
    return {}


def get_app_config():
    """Весь конфиг Tauri (api_keys + выбранные модели + STT/TTS settings) из api_keys.json."""
    p = paths.api_keys_file()
    if p.exists():
        try:
            data = json.loads(p.read_text())
            return data if isinstance(data, dict) else {}
        except Exception as e:
            log.error(f"Failed to load app config: {e}")
    return {}


def save_app_config(config: dict) -> None:
    """Save app config (models, STT/TTS settings) to api_keys.json."""
    p = paths.api_keys_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Load existing to preserve api_keys
    existing = {}
    if p.exists():
        try:
            existing = json.loads(p.read_text())
        except Exception:
            pass
    # Merge: keep existing api_keys, update other settings
    existing.update(config)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=2))
    tmp.replace(p)


def get_stt_config() -> dict:
    """Get STT configuration (model, device, compute_type)."""
    cfg = get_app_config()
    return cfg.get("stt", {
        # Final transcription: Groq first, local Whisper if the key/network is
        # unavailable. Set to "local" to force fully offline STT.
        "provider": "groq",
        "groq_model": "whisper-large-v3-turbo",
        "groq_timeout_s": 8,
        # Local fallback for the final transcript.
        "model": "small",
        # Low-latency preview captions stay local: Groq has no streaming STT
        # endpoint and its free tier allows only ~20 requests/minute.
        "partial_model": "tiny",
        "device": "cpu",
        "compute_type": "int8",
    })


DEFAULT_TTS_CONFIG = {
    # Silero is the only TTS engine. Keep the provider explicit so the API and
    # saved config never imply that another engine can be selected implicitly.
    "provider": "silero",
    "enabled": True,
    "model_id": "v4_ru",
    "speaker": "aidar",
    "language": "ru",
    "sample_rate": 48000,
    "device": "cpu",
}


def get_tts_config() -> dict:
    """Get TTS configuration, filling in defaults for older saved configs."""
    cfg = get_app_config()
    saved = cfg.get("tts")
    if not isinstance(saved, dict):
        saved = {}
    return {**DEFAULT_TTS_CONFIG, **saved}


def _spec_from(raw):
    if not isinstance(raw, dict):
        return None
    provider = raw.get("provider")
    model = raw.get("model") or raw.get("code")
    if not provider or not model:
        return None
    return {"provider": provider, "model": model}


def get_selected_models():
    """Модель, выбранная пользователем в UI. None, если выбор не сохранён."""
    cfg = get_app_config()
    return _spec_from(cfg.get("selected_primary"))
