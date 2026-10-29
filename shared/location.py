"""Resolve the user's country and expose it with an explicit confidence level.

Two independent sources, in priority order:

1. The user's own words about their life. Always wins.
2. Network geolocation by public IP. Used when the user has said nothing.

The two can disagree (a VPN, a proxy, roaming). When they do, we do not
silently pick a winner and we do not invent a third answer: the conflict is
reported so the model can mention it to the user.

Windows itself is not consulted. `Win32_ComputerSystem.SystemCountry` and
`Get-TimeZone().GeoId` come back empty on machines where the user has simply
never configured region data, and the culture/locale settings describe the
interface language rather than where the person is.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

from shared import paths

logger = logging.getLogger("location")

# Only these two are needed; the first that answers wins.
# ipapi.co was observed to return 403 without a User-Agent, so send one.
IP_ENDPOINTS = (
    "https://ipapi.co/json/",
    "https://ipinfo.io/json",
)

CACHE_TTL = 6 * 60 * 60        # re-probe at most twice a day
HTTP_TIMEOUT = 4               # seconds; never stall a turn on this

# Confidence levels, strongest first.
EXACT = "exact"        # the user stated it
INFERRED = "inferred"  # derived from public IP, possibly a VPN
UNKNOWN = "unknown"    # nothing usable


class Location:
    """A resolved country plus where that answer came from."""

    def __init__(
        self,
        level: str = UNKNOWN,
        country: str | None = None,
        country_code: str | None = None,
        source: str = "none",
        probed_at: float | None = None,
        stale: bool = False,
    ):
        self.level = level
        self.country = country
        self.country_code = country_code
        self.source = source
        self.probed_at = probed_at
        self.stale = stale

    @property
    def is_usable(self) -> bool:
        return self.level in (EXACT, INFERRED) and bool(self.country)

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "country": self.country,
            "country_code": self.country_code,
            "source": self.source,
            "probed_at": self.probed_at,
            "stale": self.stale,
        }

    def describe(self) -> str:
        """One line for the system prompt, or '' if we know nothing.

        Facts only. No instructions — the model is expected to weigh a
        low-confidence signal on its own.
        """
        if not self.is_usable:
            return "User location: unknown."

        if self.level == EXACT:
            return f"User location: {self.country} (stated by the user)."

        age = ""
        if self.stale:
            age = ", cached, probe failed"
        return (
            f"User location: {self.country} "
            f"({self.country_code or '?'}, by network IP{age})."
        )


def _cache_path() -> str:
    return str(paths.location_cache_file())


def _read_cache() -> dict:
    path = _cache_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("[LOCATION] unreadable cache: %s", e)
        return {}


def _write_cache(data: dict) -> None:
    try:
        with open(_cache_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("[LOCATION] cache write failed: %s", e)


def _probe_ip() -> tuple[str, str] | None:
    """Return (country, country_code) from the first endpoint that answers."""
    try:
        import requests
    except ImportError:
        logger.warning("[LOCATION] requests unavailable, cannot probe")
        return None

    headers = {"User-Agent": "curl/8.0", "Accept": "application/json"}
    for url in IP_ENDPOINTS:
        try:
            r = requests.get(url, timeout=HTTP_TIMEOUT, headers=headers)
            if r.status_code != 200:
                logger.info("[LOCATION] %s -> HTTP %s", url, r.status_code)
                continue
            data = r.json()
        except Exception as e:
            logger.info("[LOCATION] %s failed: %s", url, e)
            continue

        name = data.get("country_name") or data.get("country")
        code = data.get("country_code") or data.get("country_code_iso3")
        if code and len(code) == 2:
            code = code.upper()
        if name or code:
            logger.info("[LOCATION] resolved via %s: %s / %s", url, name, code)
            return (name or code, code)
    return None


_lock = threading.Lock()
_memory: Location | None = None


def get(user_stated_country: str | None = None, force: bool = False) -> Location:
    """Resolve location, preferring the user's own statement.

    Cheap by construction: after the first successful probe the answer is served
    from disk, so this costs one dict read and no network on subsequent turns.
    """
    global _memory

    # The user's words about their own life outrank anything we can measure.
    if user_stated_country:
        return Location(
            level=EXACT,
            country=user_stated_country.strip(),
            source="user_statement",
        )

    with _lock:
        now = time.time()
        if not force and _memory is not None and now - (_memory.probed_at or 0) < CACHE_TTL:
            return _memory

        cache = _read_cache()
        probed_at = cache.get("probed_at") or 0
        if not force and cache.get("country") and now - probed_at < CACHE_TTL:
            loc = Location(
                level=INFERRED,
                country=cache.get("country"),
                country_code=cache.get("country_code"),
                source="ip_cache",
                probed_at=probed_at,
            )
            _memory = loc
            return loc

        hit = _probe_ip()
        if not hit:
            # Keep the previous answer rather than forgetting it, but mark it
            # stale so the model knows it is not fresh.
            loc = Location(
                level=INFERRED if cache.get("country") else UNKNOWN,
                country=cache.get("country"),
                country_code=cache.get("country_code"),
                source="ip_cache_stale" if cache.get("country") else "unavailable",
                probed_at=probed_at or None,
                stale=True,
            )
            _memory = loc
            return loc

        country, code = hit
        _write_cache({
            "country": country,
            "country_code": code,
            "probed_at": now,
        })
        loc = Location(
            level=INFERRED,
            country=country,
            country_code=code,
            source="ip",
            probed_at=now,
        )
        _memory = loc
        return loc
