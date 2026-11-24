import json
import logging
import time
from pathlib import Path
from shared.paths import app_dir

logger = logging.getLogger("key_manager")


class RateLimitStore:
    """Persistent store for TPD (Tokens Per Day) and RPM (Requests Per Minute) tracking."""

    def __init__(self):
        self._path = app_dir() / "cache" / "rate_limits.json"
        self._data = self._load()

    def _load(self) -> dict:
        if not self._path.is_file():
            return {"providers": {}}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[RateLimitStore] Failed to load: {e}")
            return {"providers": {}}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except Exception as e:
            logger.warning(f"[RateLimitStore] Failed to save: {e}")

    def record_request(self, provider: str, tokens: int = 0) -> None:
        """Record a request and token usage for a provider."""
        now = time.time()
        day_key = time.strftime("%Y-%m-%d", time.localtime(now))
        minute_key = time.strftime("%Y-%m-%d-%H-%M", time.localtime(now))

        provider_data = self._data["providers"].setdefault(provider, {"daily": {}, "minute": {}})

        # Daily tokens
        provider_data["daily"][day_key] = provider_data["daily"].get(day_key, 0) + tokens
        provider_data["daily"]["requests"] = provider_data["daily"].get("requests", 0) + 1

        # Minute requests
        provider_data["minute"][minute_key] = provider_data["minute"].get(minute_key, 0) + 1

        # Cleanup old entries (keep last 2 days / 2 hours)
        cutoff_day = time.time() - 2 * 86400
        cutoff_minute = time.time() - 2 * 3600
        provider_data["daily"] = {
            k: v for k, v in provider_data["daily"].items()
            if k == "requests" or time.mktime(time.strptime(k, "%Y-%m-%d")) > cutoff_day
        }
        provider_data["minute"] = {
            k: v for k, v in provider_data["minute"].items()
            if time.mktime(time.strptime(k, "%Y-%m-%d-%H-%M")) > cutoff_minute
        }

        self._save()

    def get_daily_tokens(self, provider: str) -> int:
        """Get total tokens used today for a provider."""
        day_key = time.strftime("%Y-%m-%d")
        provider_data = self._data["providers"].get(provider, {})
        return provider_data.get("daily", {}).get(day_key, 0)

    def get_daily_requests(self, provider: str) -> int:
        """Get total requests made today for a provider."""
        provider_data = self._data["providers"].get(provider, {})
        return provider_data.get("daily", {}).get("requests", 0)

    def get_minute_requests(self, provider: str) -> int:
        """Get requests in current minute for a provider."""
        minute_key = time.strftime("%Y-%m-%d-%H-%M")
        provider_data = self._data["providers"].get(provider, {})
        return provider_data.get("minute", {}).get(minute_key, 0)

    def get_remaining_daily(self, provider: str, limit: int) -> int:
        """Get remaining daily token budget."""
        used = self.get_daily_tokens(provider)
        return max(0, limit - used)

    def get_remaining_minute(self, provider: str, limit: int) -> int:
        """Get remaining minute request budget."""
        used = self.get_minute_requests(provider)
        return max(0, limit - used)

    def is_daily_exhausted(self, provider: str, limit: int) -> bool:
        """Check if daily token limit is exhausted."""
        return self.get_daily_tokens(provider) >= limit

    def is_minute_exhausted(self, provider: str, limit: int) -> bool:
        """Check if minute request limit is exhausted."""
        return self.get_minute_requests(provider) >= limit


# Global rate limit store instance
_rate_limit_store: RateLimitStore | None = None


def get_rate_limit_store() -> RateLimitStore:
    global _rate_limit_store
    if _rate_limit_store is None:
        _rate_limit_store = RateLimitStore()
    return _rate_limit_store


class APIKeyManager:
    """Pool of Groq API keys with round-robin rotation on rate limits.

    On 429: immediately rotate to the next key.
    Stop only when ALL keys are exhausted.
    """

    def __init__(self, keys: list[str]):
        if not keys:
            raise ValueError("At least one API key is required")
        self._keys = list(keys)
        self._index = 0
        self._rate_store = get_rate_limit_store()
        logger.info(f"[KEY_MANAGER] Initialized with {len(self._keys)} keys")

    def get_key(self) -> str:
        """Return the current active key."""
        return self._keys[self._index]

    def get_client(self):
        """Return a Groq client initialized with the current key."""
        from groq import Groq
        return Groq(api_key=self._keys[self._index])

    def rotate(self) -> str | None:
        """Switch to the next key. Return it, or None if all exhausted."""
        self._index += 1
        if self._index >= len(self._keys):
            logger.error("[KEY_MANAGER] All keys exhausted")
            return None
        key = self._keys[self._index]
        logger.warning(
            f"[KEY_MANAGER] Rotated to key ...{key[-8:]} "
            f"({self._index + 1}/{len(self._keys)})"
        )
        return key

    def all_exhausted(self) -> bool:
        """True when no keys are left."""
        return self._index >= len(self._keys)

    def current_index(self) -> int:
        return self._index

    def total_keys(self) -> int:
        return len(self._keys)

    # --- Rate limit tracking methods ---

    def record_request(self, provider: str, tokens: int = 0) -> None:
        """Record a request for rate limit tracking."""
        self._rate_store.record_request(provider, tokens)

    def get_daily_tokens(self, provider: str) -> int:
        return self._rate_store.get_daily_tokens(provider)

    def get_daily_requests(self, provider: str) -> int:
        return self._rate_store.get_daily_requests(provider)

    def get_minute_requests(self, provider: str) -> int:
        return self._rate_store.get_minute_requests(provider)

    def get_remaining_daily(self, provider: str, limit: int) -> int:
        return self._rate_store.get_remaining_daily(provider, limit)

    def get_remaining_minute(self, provider: str, limit: int) -> int:
        return self._rate_store.get_remaining_minute(provider, limit)

    def is_daily_exhausted(self, provider: str, limit: int) -> bool:
        return self._rate_store.is_daily_exhausted(provider, limit)

    def is_minute_exhausted(self, provider: str, limit: int) -> bool:
        return self._rate_store.is_minute_exhausted(provider, limit)