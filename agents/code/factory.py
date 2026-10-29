import logging
import os

import config.code_agent as config_code
from shared.config_loader import get_api_keys, get_app_config
from agents.main.agent import Agent, AgentConfig
from shared.llm_client import LLMClient
from shared.llm_client.api_key_manager import APIKeyManager
from agents.code.registry import CodeToolRegistry

logger = logging.getLogger("code.factory")

PROVIDER_DEFAULT_MODELS = {
    "groq": "openai/gpt-oss-120b",
    "google": "gemini-1.5-flash",
    "nim": "nvidia/nemotron-3-ultra",
    "openai": "gpt-4o-mini",
    # Empty -> factory fetches the first live chat model from OpenRouter.
    "openrouter": "",
}


def _resolve_key_for_provider(
    provider: str,
    api_keys,
    api_key: str | None,
    all_keys_from_loader: dict,
    settings,
) -> str | None:
    if not provider:
        provider = "groq"

    if isinstance(api_keys, dict) and provider in api_keys:
        entry = api_keys[provider]
        if isinstance(entry, dict):
            k = entry.get("key")
            if k:
                return k
        elif isinstance(entry, str) and entry:
            return entry

    if isinstance(api_keys, list) and provider == "groq":
        for k in api_keys:
            if isinstance(k, str) and k:
                return k

    if isinstance(all_keys_from_loader, dict) and provider in all_keys_from_loader:
        entry = all_keys_from_loader[provider]
        if isinstance(entry, dict):
            k = entry.get("key")
            if k:
                return k
        elif isinstance(entry, str) and entry:
            return entry

    if isinstance(api_key, str) and api_key:
        return api_key

    settings_key_map = {
        "groq": getattr(settings, "groq_api_key", None),
        "google": getattr(settings, "google_api_key", None),
        "nim": getattr(settings, "nim_api_key", None),
        "openrouter": getattr(settings, "openrouter_api_key", None),
    }
    if provider in settings_key_map:
        k = settings_key_map[provider]
        if k:
            return k

    fallback_env = {
        "groq": os.getenv("GROQ_API_KEY"),
        "google": os.getenv("GOOGLE_API_KEY"),
        "nim": os.getenv("NIM_API_KEY"),
        "openai": os.getenv("OPENAI_API_KEY"),
        "openrouter": os.getenv("OPENROUTER_API_KEY"),
    }
    return fallback_env.get(provider)


def _fetch_live_default_model(provider: str, key: str, base_url: str | None = None) -> str | None:
    try:
        from shared.llm_client.model_fetcher import fetch_provider_models
        live_models = fetch_provider_models(provider, key, base_url)
        if live_models:
            sorted_models = sorted(live_models)
            return sorted_models[0]
    except Exception as e:
        logger.warning(f"[CODE FACTORY] Failed to fetch live models for {provider}: {e}")
    return None


def _make_llm_for_model(
    model: str,
    provider: str,
    api_keys,
    api_key: str | None,
    all_keys: dict,
    settings,
    base_url: str | None = None,
) -> LLMClient | None:
    if not provider:
        raise ValueError("Code agent: provider must be passed explicitly (auto-detect removed).")
    
    key = _resolve_key_for_provider(provider, api_keys, api_key, all_keys, settings)
    if not key:
        raise ValueError(f"No API key found for provider '{provider}' (model='{model}').")

    if not model:
        live_default = _fetch_live_default_model(provider, key, base_url)
        if live_default:
            model = live_default
            logger.info("[CODE FACTORY] Model not specified, dynamically fetched and selected '%s' for provider '%s'", model, provider)
        else:
            model = "default"
            logger.warning("[CODE FACTORY] Model not specified and live fetch failed (offline?), using fallback 'default'")

    manager = APIKeyManager([key])
    return LLMClient(key_manager=manager, model=model, base_url=base_url, provider=provider)


def _resolve_model_provider_from_config(
    config,
    settings,
    fallback_model: str,
    fallback_provider: str,
):
    """
    Разбирает config.selected_primary (текущая схема Tauri UI), затем legacy
    config.selected_model / selected_provider, иначе дефолты из settings
    (JARVIS_CODE_PROVIDER).
    """
    model, provider = None, None
    if config and isinstance(config, dict):
        # What AppConfig.selected_primary actually persists in api_keys.json.
        # This wins: it is what the settings UI writes last.
        sp = config.get("selected_primary")
        if isinstance(sp, dict) and (sp.get("model") or sp.get("provider")):
            provider = sp.get("provider") or None
            model = sp.get("model") or None
        else:
            # Legacy shape, only when there is no current one.
            sm = config.get("selected_model")
            if isinstance(sm, dict) and (sm.get("code") or sm.get("model")):
                model = sm.get("code") or sm.get("model")
                provider = sm.get("provider") or None
            legacy = config.get("selected_provider")
            if legacy:
                provider = legacy

    if not model and hasattr(settings, "get_model"):
        model = settings.get_model()
    if not provider:
        provider = getattr(settings, "provider", None)

    return (model or fallback_model), (provider or fallback_provider)


def _build_tool_directory(registry: CodeToolRegistry) -> str:
    """Render the prompt's tool list from the registry's *enabled* tools.

    Must be generated from the same set the LLM schema exposes: a tool named in
    the prompt but missing from `tools=` makes the model emit phantom calls and
    the verifier retry forever. Lazy tools are listed with their purpose so the
    model knows they exist even though their schema is not sent upfront.
    """
    return registry.get_prompt_directory()


def create_code_agent(
    project_path: str,
    api_keys: dict | None = None,
    api_key: str | None = None,
    system_prompt: str = "",
    primary_model: str | None = "",
    primary_provider: str | None = "",
    config: dict | None = None,
    event_sink=None,
    base_url: str | None = None,
    disabled_tools: list[str] | None = None,
) -> Agent:
    settings = config_code.Settings.from_env(project_path)
    all_keys = get_api_keys()

    # 1) Primary: явные аргументы → сохранённый выбор UI → settings.get_model()/settings.provider
    if not primary_provider or not primary_model:
        if not config:
            # Honour the model/provider the user picked in the settings UI even
            # when the caller does not forward the app config.
            try:
                config = get_app_config()
            except Exception:
                config = None
        fb_p = primary_provider or "groq"
        fb_m = primary_model or PROVIDER_DEFAULT_MODELS.get(fb_p, "openai/gpt-oss-120b")
        resolved_m, resolved_p = _resolve_model_provider_from_config(
            config, settings, fb_m, fb_p
        )
        if not primary_model:
            primary_model = resolved_m
        if not primary_provider:
            primary_provider = resolved_p

    logger.info(
        "[CODE FACTORY] Creating code agent — primary %s:%s",
        primary_provider, primary_model,
    )

    primary_llm = _make_llm_for_model(
        primary_model, primary_provider, api_keys, api_key, all_keys, settings, base_url
    )

    registry = CodeToolRegistry(
        project_path=settings.project_path, disabled=disabled_tools
    )
    prompt = system_prompt or config_code.SYSTEM_PROMPT.format(
        project_path=settings.project_path,
        tool_directory=_build_tool_directory(registry),
    )

    return Agent(
        registry=registry,
        primary_llm=primary_llm,
        system_prompt=prompt,
        config=AgentConfig(
            max_iterations=15,
            max_tool_calls=40,
            max_execution_time=180,
        ),
        event_sink=event_sink,
    )
