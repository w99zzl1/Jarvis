"""Rate limit tracking and retry logic for the agent."""

import logging
import re
import time
from dataclasses import dataclass

logger = logging.getLogger("agent.rate_limiter")


# A Google 429 is normally a *per-minute* throttle: the body carries a
# `retryDelay` telling us exactly how long to wait, so the turn can continue.
# A per-day quota is different - waiting is pointless. These patterns tell the
# two apart and pull the delay out of the raw provider error string.
_RETRY_DELAY_RE = re.compile(
    r"retrydelay[\"']?\s*[:\s]\s*[\"']?([0-9]+(?:\.[0-9]+)?)s", re.IGNORECASE
)
_RETRY_IN_RE = re.compile(r"retry in ([0-9]+(?:\.[0-9]+)?)s", re.IGNORECASE)
_PER_DAY_RE = re.compile(r"per\s*day", re.IGNORECASE)


@dataclass
class RateLimitInfo:
    delay: float
    per_day: bool


def parse_rate_limit(message: str | None) -> RateLimitInfo | None:
    """Decode a provider 429 body: how long to wait, and whether it is per-day."""
    if not message:
        return None
    per_day = bool(_PER_DAY_RE.search(message))
    match = _RETRY_DELAY_RE.search(message) or _RETRY_IN_RE.search(message)
    delay = float(match.group(1)) if match else 5.0
    return RateLimitInfo(delay=delay, per_day=per_day)


def get_retry_delay(error: str) -> float:
    """Return the number of seconds to wait before a retry attempt.

    Dynamic delays based on error type:
    - rate_limited: longer wait (5s) to respect API RPM limits
    - tool_execution errors: medium wait (2s)
    - other errors: short wait (1s)
    """
    error_lower = (error or "").lower()
    if "rate_limited" in error_lower:
        return 5.0
    # Provider capacity problems (Gemini 503 UNAVAILABLE, overloaded) need a
    # real pause — retrying after 1s just hits the same limit.
    if "server" in error_lower or "503" in error_lower or "unavailable" in error_lower:
        return 8.0
    if "timeout" in error_lower or "connection" in error_lower:
        return 3.0
    # Default short delay for other errors
    return 1.0


class RateLimiter:
    """Tracks rate limit hits and manages retry behavior."""

    def __init__(self, config):
        self.config = config
        self.rate_limit_hits = 0
        self.aggressive_trim = False

    def record_hit(self):
        """Record a rate limit hit and update aggressive trim state."""
        self.rate_limit_hits += 1
        if self.rate_limit_hits >= 2:
            self.aggressive_trim = True
            logger.info(
                "[RATE_LIMITER] Repeated rate limits (%d); enabling "
                "aggressive context trimming for this session.",
                self.rate_limit_hits,
            )

    def should_retry(self, info: RateLimitInfo | None, rl_retries: int, rl_waited: float) -> tuple[bool, float]:
        """Determine if we should retry and how long to wait.

        Returns:
            Tuple of (should_retry: bool, wait_seconds: float)
        """
        if (
            info is None or info.per_day
            or rl_retries >= max(1, self.config.rate_limit_retries)
            or rl_waited >= max(30.0, self.config.rate_limit_wait)
        ):
            return False, 0.0

        wait = min(max(info.delay, 1.0) + 1.5, max(30.0, self.config.rate_limit_wait) - rl_waited)
        return True, wait

    def is_exhausted(self) -> bool:
        """Check if rate limit retries are exhausted."""
        return self.rate_limit_hits >= max(1, self.config.rate_limit_retries)

    def get_state(self) -> dict:
        """Get current state for serialization."""
        return {
            "rate_limit_hits": self.rate_limit_hits,
            "aggressive_trim": self.aggressive_trim,
        }

    def set_state(self, state: dict):
        """Restore state from serialization."""
        self.rate_limit_hits = state.get("rate_limit_hits", 0)
        self.aggressive_trim = state.get("aggressive_trim", False)