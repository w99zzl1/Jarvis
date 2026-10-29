import logging

logger = logging.getLogger("key_manager")


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