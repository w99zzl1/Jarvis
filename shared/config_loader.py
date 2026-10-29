import os
import pathlib
import logging
import json

log = logging.getLogger("config_loader")

def get_config_dir():
    # Исправляем путь: Tauri использует com.jarvis.assistant
    appdata = os.environ.get("APPDATA")
    if appdata:
        # Пытаемся найти папку, которую реально использует Tauri
        path = pathlib.Path(appdata) / "com.jarvis.assistant"
        if not path.exists():
            # Если такой нет, fallback на старую логику или создание
            path = pathlib.Path(appdata) / "jarvis"
        return path
    return pathlib.Path.cwd() 

def get_api_keys():
    p = get_config_dir() / "api_keys.json"
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
    p = get_config_dir() / "api_keys.json"
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
