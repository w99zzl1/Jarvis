import logging
import re

import httpx

logger = logging.getLogger("model_fetcher")

# The agent talks to exactly one endpoint: chat.completions (OpenAI-compatible)
# or generate_content (Google). Image, speech, transcription, embedding,
# rerank and moderation models cannot be called that way - they answer HTTP 400
# and the agent dies with a bare "fatal". They are noise in a chat selector.
#
# Google and Groq advertise the capability, so those branches filter on the
# provider's own metadata. OpenAI/NIM do not, so they fall back to this list.
_NON_CHAT_PATTERNS = re.compile(
    r"("
    r"dall-e|imagen|stable-diffusion|flux|cogview|nano-banana|-image|"
    r"tts|whisper|alloy|shimmer|bark|playht|orpheus|transcribe|lyria|"
    r"text-embedding|embedding|embed-|bge|embedqa|embedde|"
    r"rerank|rerankqa|"
    r"omni-moderation|moderation|"
    r"babbage|davinci|cda-|text-curious|turbo-instruct|"
    r"audio-|realtime|visionqa|nv-embed|"
    r"guard|safeguard|deep-research|robotics|computer-use"
    r")",
    re.IGNORECASE,
)

# Google exposes niche models that do generate text but are not general
# assistants; they only add noise to the list.
_GOOGLE_DENY = re.compile(r"^(medlm-|embeddinggemma-|aqa-)", re.IGNORECASE)


def _is_chat_model(provider: str, model_id: str, item: dict) -> bool:
    """Keep a model only if the agent can actually call it as a chat model."""
    mid = model_id.lower()

    if provider == "google":
        # Authoritative part: the API states which methods the model supports.
        methods = item.get("supportedGenerationMethods") or []
        if methods and "generateContent" not in methods:
            return False
        # generateContent is not the same as "a chat assistant": image, tts,
        # music, guard and research models expose it too, so the name filter
        # still has to run.
        if _NON_CHAT_PATTERNS.search(mid):
            return False
        return not _GOOGLE_DENY.match(mid)

    if provider == "groq":
        # Groq tags non-chat entries: "openai/whisper", "openai/guard", ...
        if (item.get("type") or "model") != "model":
            return False
        # Some safety models are still tagged type=model, so check the name too.
        return not _NON_CHAT_PATTERNS.search(mid)

    return not _NON_CHAT_PATTERNS.search(mid)


def _is_preview(model_id: str) -> bool:
    """Preview/experimental builds still work as chat models but are unstable,
    so the UI groups them separately instead of hiding them."""
    return bool(re.search(r"(^|-)preview|exp|experimental|latest", model_id, re.IGNORECASE))


def fetch_provider_models(provider: str, api_key: str, base_url: str | None = None) -> list[str]:
    """Returns the chat-capable models of a provider via its official API."""
    if not api_key:
        return []

    provider = provider.lower().strip()
    models: list[str] = []

    try:
        if provider == "groq":
            url = "https://api.groq.com/openai/v1/models"
            headers = {"Authorization": f"Bearer {api_key}"}
            resp = httpx.get(url, headers=headers, timeout=10.0)
            if resp.status_code == 200:
                data = resp.json()
                for item in data.get("data", []):
                    mid = item.get("id")
                    if mid and _is_chat_model(provider, mid, item):
                        models.append(mid)
            else:
                logger.warning(f"[ModelFetcher] Groq returned status {resp.status_code}: {resp.text}")

        elif provider == "openai":
            url = "https://api.openai.com/v1/models"
            headers = {"Authorization": f"Bearer {api_key}"}
            resp = httpx.get(url, headers=headers, timeout=10.0)
            if resp.status_code == 200:
                data = resp.json()
                for item in data.get("data", []):
                    mid = item.get("id")
                    if mid and _is_chat_model(provider, mid, item):
                        models.append(mid)
            else:
                logger.warning(f"[ModelFetcher] OpenAI returned status {resp.status_code}: {resp.text}")

        elif provider == "nim":
            url = base_url or "https://integrate.api.nvidia.com/v1/models"
            if not url.endswith("/models"):
                url = url.rstrip("/") + "/models"
            headers = {"Authorization": f"Bearer {api_key}"}
            resp = httpx.get(url, headers=headers, timeout=10.0)
            if resp.status_code == 200:
                data = resp.json()
                for item in data.get("data", []):
                    mid = item.get("id")
                    if mid and _is_chat_model(provider, mid, item):
                        models.append(mid)
            else:
                logger.warning(f"[ModelFetcher] NIM returned status {resp.status_code}: {resp.text}")

        elif provider == "openrouter":
            url = "https://openrouter.ai/api/v1/models"
            headers = {"Authorization": f"Bearer {api_key}"}
            resp = httpx.get(url, headers=headers, timeout=10.0)
            if resp.status_code == 200:
                data = resp.json()
                for item in data.get("data", []):
                    mid = item.get("id")
                    if mid and _is_chat_model(provider, mid, item):
                        models.append(mid)
            else:
                logger.warning(f"[ModelFetcher] OpenRouter returned status {resp.status_code}: {resp.text}")

        elif provider == "openai_compat":
            if base_url:
                url = base_url.rstrip("/") + "/models"
                headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
                resp = httpx.get(url, headers=headers, timeout=10.0)
                if resp.status_code == 200:
                    data = resp.json()
                    for item in data.get("data", []):
                        mid = item.get("id")
                        if mid and _is_chat_model(provider, mid, item):
                            models.append(mid)

        elif provider == "google":
            url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
            resp = httpx.get(url, timeout=10.0)
            if resp.status_code == 200:
                data = resp.json()
                for m in data.get("models", []):
                    name = m.get("name", "")
                    mid = name.replace("models/", "") if name else ""
                    if mid and _is_chat_model(provider, mid, m):
                        models.append(mid)
            else:
                logger.warning(f"[ModelFetcher] Google returned status {resp.status_code}: {resp.text}")

    except Exception as e:
        logger.warning(f"[ModelFetcher] Error fetching models for {provider}: {e}")

    # Deduplicate while preserving provider order.
    seen: set[str] = set()
    unique: list[str] = []
    for m in models:
        if m not in seen:
            seen.add(m)
            unique.append(m)
    return unique


def check_connected_providers_and_fetch_models(api_keys_dict: dict | None = None) -> dict[str, list[str]]:
    """
    Проверяет список подключенных провайдеров и по каждому запрашивает
    список предоставляемых им моделей через официальный API.
    """
    import os
    import config.main as config_main
    
    # Сбор ключей из разных источников
    keys = {}
    base_urls = {}
    if isinstance(api_keys_dict, dict):
        for p, entry in api_keys_dict.items():
            if isinstance(entry, dict) and entry.get("key"):
                keys[p] = entry.get("key")
                if entry.get("base_url"):
                    base_urls[p] = entry["base_url"]
            elif isinstance(entry, str) and entry:
                keys[p] = entry

    # Добавляем дефолтные/env ключи
    if "groq" not in keys:
        g_key = config_main.GROQ_API_KEY or os.environ.get("GROQ_API_KEY")
        if g_key:
            keys["groq"] = g_key
    if "google" not in keys:
        go_key = config_main.GOOGLE_API_KEY or os.environ.get("GOOGLE_API_KEY")
        if go_key:
            keys["google"] = go_key
    if "openai" not in keys:
        o_key = os.environ.get("OPENAI_API_KEY")
        if o_key:
            keys["openai"] = o_key
    if "nim" not in keys:
        n_key = os.environ.get("NIM_API_KEY")
        if n_key:
            keys["nim"] = n_key
    if "openrouter" not in keys:
        or_key = getattr(config_main, "OPENROUTER_API_KEY", None) or os.environ.get("OPENROUTER_API_KEY")
        if or_key:
            keys["openrouter"] = or_key

    logger.info(f"[ModelFetcher] Checking connected providers: {list(keys.keys())}")

    result = {}
    for provider, key in keys.items():
        if key:
            models = fetch_provider_models(provider, key, base_url=base_urls.get(provider))
            if models:
                # Stable first, preview last: the model list is a picker, and the
                # reliable choices should not be buried under experimental builds.
                models.sort(key=lambda m: (_is_preview(m), m.lower()))
                result[provider] = models
                logger.info(f"[ModelFetcher] Provider '{provider}' connected, fetched {len(models)} chat models.")
            else:
                logger.warning(f"[ModelFetcher] Provider '{provider}' has key but returned 0 models.")
        else:
            logger.info(f"[ModelFetcher] Provider '{provider}' has no API key.")

    return result
