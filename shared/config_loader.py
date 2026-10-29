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
    """Весь конфиг Tauri (api_keys + выбранные модели) из api_keys.json."""
    p = paths.api_keys_file()
    if p.exists():
        try:
            data = json.loads(p.read_text())
            return data if isinstance(data, dict) else {}
        except Exception as e:
            log.error(f"Failed to load app config: {e}")
    return {}


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
