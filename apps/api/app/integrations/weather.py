"""Weather lookup — Open-Meteo (keyless) current temp / overnight low /
heat-warning flag for the property's location (#30).

Why Open-Meteo
--------------
Keyless, free, no signup, no credential to provision — `architecture.md`
does not name a specific weather vendor for this lookup (it only specifies
the SHAPE the rubric needs: current temp, overnight low, heat-warning flag —
see ``docs/02-product/severity-rubric-v1.md``'s "Notes for implementation").
Open-Meteo is chosen here as the concrete provider precisely because it
needs no ``fly secrets set`` step — "things humans must do" in
``apps/api/CLAUDE.md`` explicitly calls out credential provisioning as a
human task, and this issue can ship without asking for one.

Heat-warning approximation (a documented deviation, not a hidden guess)
------------------------------------------------------------------------
The rubric's "official heat warning" (Environment Canada issues these for
Southern Ontario, roughly: forecast daytime high >= 31 C for two or more
consecutive days, or a humidex >= 40) is NOT a feed Open-Meteo's free,
keyless forecast endpoint exposes — there is no generic "active government
heat alert" field to read. Rather than silently omitting the flag (which
would starve the rubric's "no AC during an official heat warning" URGENT
rule of any signal at all), this module approximates it with a single,
documented threshold: ``heat_warning=True`` when the property's forecasted
DAILY HIGH for today is >= ``_HEAT_WARNING_MAX_TEMP_C`` (31.0 C, matching
Environment Canada's common Southern-Ontario daytime threshold). This is a
deliberate, conservative approximation — flagged here for the record and in
the issue report as a candidate for a real alerts integration later (e.g. a
paid weather-alerts API, or scraping Environment Canada's public alert
feed), not something to silently trust as authoritative.

Overnight-low approximation
----------------------------
"Overnight low" uses the MINIMUM of Open-Meteo's daily minimum-temperature
forecast across TODAY and TOMORROW (``forecast_days=2``,
``min(daily.temperature_2m_min[0:2])``) rather than picking out the
specific overnight hours from an hourly series — a reasonable v1
simplification (documented, not hidden). Using ONLY today's minimum
(``[0]``) has a real bug shape: run this in the evening and today's daily
minimum is almost always this MORNING's low (already happened, often much
warmer than tonight will be) — the classifier would see a stale, warm
number precisely when the actual overnight cold is still ahead of it. Two
days is not by itself a semantically correct "the next 24h from right now"
either, but taking the min across today+tomorrow instead of just today
means the reading can only go COLDER (or stay the same), never falsely
warmer than the truth — consistent with the rubric's bias rule (escalate
when uncertain; never round down) applied to the INPUT rather than the
classification itself. A more precise implementation would use the hourly
series plus the property's local timezone to isolate the next overnight
window exactly; left as a follow-up, not required by the rubric's "current
or forecast overnight low" phrasing, which already anticipates using
whichever of the two is available.

Availability contract
----------------------
``get_weather_snapshot`` NEVER raises and NEVER blocks longer than
``_TIMEOUT_SECONDS`` (a tight 3 s budget — this must not meaningfully delay
``load_context``, and the rubric's own bias rule already covers a missing
weather reading by escalating when uncertain, per severity-rubric-v1.md).
Any failure — timeout, connection error, non-2xx status, or an
unexpected/malformed response body — is caught, logged (uuids/rounded
coordinates only, never anything else), and turned into ``None``. Callers
(``app/agent/nodes/load_context.py``) must treat ``None`` as "weather
unavailable" and proceed with classification regardless (never block, never
raise out of the node).

Caching
-------
An in-process TTL cache (``_CACHE_TTL_SECONDS`` = 30 minutes, meeting the
issue's ">= 30 min" requirement), keyed on lat/lon ROUNDED to
``_COORD_ROUND_NDIGITS`` (2 decimal places, ~1.1 km at Toronto's latitude —
plenty of precision for a weather lookup, and keeps distinct properties a
few doors apart from needlessly missing each other's cache entry). Rounded
coordinates are also the only location data this module ever logs (never
raw/full-precision lat/lon, and certainly never a tenant phone number or
address) — see never-break rule #5.

Not thundering-herd-safe: two concurrent cache-miss lookups for the same
property will each make their own upstream request rather than coalescing
into one (unlike ``app/integrations/supabase_auth.py``'s JWKS cache, which
uses an ``asyncio.Lock`` for exactly this). Accepted as a documented
simplification for v1 — weather lookups are cheap, free, and gated by a
30-minute cache; add a lock here if traffic ever makes the duplicate calls
a real cost.

Testing seam
------------
``_now()`` wraps ``time.monotonic()`` (identical pattern to
``app/integrations/supabase_auth.py``'s ``_now()``) so tests can monkeypatch
a single function to simulate cache expiry without real ``sleep()`` calls.
The module-level ``_cache_state`` is reset between tests by
``tests/conftest.py``'s autouse fixture, for the same cross-test-leakage
reason documented there for the JWKS cache and the checkpointer pool.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

from app.agent.schemas import WeatherSnapshot

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE_URL = "https://api.open-meteo.com/v1/forecast"

_TIMEOUT_SECONDS: float = 3.0
"""Tight, per the issue: a slow/hanging weather provider must never
meaningfully delay ``load_context`` / classification."""

_CACHE_TTL_SECONDS: float = 30 * 60
"""30 minutes — the issue's ">= 30 min" minimum, used exactly (no slack
added; a shorter TTL would violate the AC, a much longer one would go stale
against real weather swings)."""

_COORD_ROUND_NDIGITS: int = 2
"""~1.1 km of precision at mid-latitudes — see module docstring."""

_HEAT_WARNING_MAX_TEMP_C: float = 31.0
"""Documented approximation for "official heat warning" — see module
docstring "Heat-warning approximation"."""

_CoordKey = tuple[float, float]


def _now() -> float:
    """Monotonic clock seam — see module docstring "Testing seam"."""
    return time.monotonic()


class _WeatherCacheState:
    """Mutable module-level TTL cache — a single object so there is one
    thing to reset between tests (mirrors
    ``app/integrations/supabase_auth.py``'s ``_JwksState`` pattern)."""

    def __init__(self) -> None:
        self.entries: dict[_CoordKey, tuple[WeatherSnapshot, float]] = {}

    def reset_for_tests(self) -> None:
        self.entries = {}


_cache_state = _WeatherCacheState()


def _cache_key(lat: float, lon: float) -> _CoordKey:
    return (round(lat, _COORD_ROUND_NDIGITS), round(lon, _COORD_ROUND_NDIGITS))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def get_weather_snapshot(lat: float | None, lon: float | None) -> WeatherSnapshot | None:
    """Return current temp / overnight low / heat-warning flag for
    ``(lat, lon)``, or ``None`` when unavailable.

    ``None`` in, ``None`` out: a property with no coordinates on file
    (``properties.lat``/``lon`` are nullable, schema-v1.md) has no location
    to look weather up for — this is the documented "weather unavailable"
    path, not an error.

    Never raises; never blocks longer than ``_TIMEOUT_SECONDS`` plus
    negligible local work. See module docstring for the full availability
    contract and the cache design.
    """
    if lat is None or lon is None:
        return None

    key = _cache_key(lat, lon)
    now = _now()

    cached = _cache_state.entries.get(key)
    if cached is not None:
        cached_snapshot, cached_at = cached
        if now - cached_at < _CACHE_TTL_SECONDS:
            return cached_snapshot

    fetched_snapshot = await _fetch(key[0], key[1])
    if fetched_snapshot is not None:
        _cache_state.entries[key] = (fetched_snapshot, now)
    return fetched_snapshot


# ---------------------------------------------------------------------------
# Internal — the actual Open-Meteo call
# ---------------------------------------------------------------------------


async def _fetch(lat: float, lon: float) -> WeatherSnapshot | None:
    """One Open-Meteo forecast request for the (already-rounded) coordinate.

    Catches every failure mode (timeout, connection error, non-2xx status,
    malformed/unexpected JSON body) into a single logged ``None`` return —
    see module docstring "Availability contract". Only rounded coordinates
    are ever logged, never anything else (rule #5).
    """
    params: dict[str, Any] = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m",
        "daily": "temperature_2m_min,temperature_2m_max",
        "forecast_days": 2,
        "timezone": "auto",
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.get(_BASE_URL, params=params)
            response.raise_for_status()
            data: dict[str, Any] = response.json()

        current_temp_c = float(data["current"]["temperature_2m"])
        # Overnight low = min across TODAY and TOMORROW -- see module
        # docstring "Overnight-low approximation" for why just [0] alone
        # can under-report tonight's cold with a stale morning reading.
        daily_lows = data["daily"]["temperature_2m_min"][0:2]
        overnight_low_c = float(min(daily_lows))
        daily_max_c = float(data["daily"]["temperature_2m_max"][0])
        heat_warning = daily_max_c >= _HEAT_WARNING_MAX_TEMP_C

        return WeatherSnapshot(
            current_temp_c=current_temp_c,
            overnight_low_c=overnight_low_c,
            heat_warning=heat_warning,
        )
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        log.warning(
            "weather_lookup_unavailable",
            exc_type=type(exc).__name__,
            lat=lat,
            lon=lon,
        )
        return None


__all__: list[str] = ["get_weather_snapshot"]
