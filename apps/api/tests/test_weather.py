"""Unit tests for app/integrations/weather.py (#30).

Markers: ``@pytest.mark.unit`` — mocked httpx via ``respx``; NO real
network access, ever (Open-Meteo is mocked in every test here).
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.agent.schemas import WeatherSnapshot
from app.integrations import weather as weather_mod

_URL = weather_mod._BASE_URL

_TORONTO_LAT = 43.6532
_TORONTO_LON = -79.3832


def _success_body(
    current_temp: float,
    low_today: float,
    high: float,
    *,
    low_tomorrow: float | None = None,
) -> dict[str, object]:
    """Build an Open-Meteo-shaped response body. Defaults to a single-day
    ``temperature_2m_min`` array (so existing single-value assertions keep
    working — ``min([x]) == x``); pass ``low_tomorrow`` to exercise the
    two-day-minimum behaviour (#110 review item 5)."""
    lows = [low_today] if low_tomorrow is None else [low_today, low_tomorrow]
    return {
        "current": {"temperature_2m": current_temp},
        "daily": {
            "temperature_2m_min": lows,
            "temperature_2m_max": [high],
        },
    }


# ---------------------------------------------------------------------------
# Nullable lat/lon -> unavailable, no network attempted
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_missing_coordinates_returns_none_without_network() -> None:
    with respx.mock(assert_all_called=False) as respx_mock:
        route = respx_mock.get(_URL)
        result = await weather_mod.get_weather_snapshot(None, None)
        assert result is None
        assert route.call_count == 0


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_success_returns_weather_snapshot() -> None:
    with respx.mock() as respx_mock:
        respx_mock.get(_URL).mock(
            return_value=httpx.Response(200, json=_success_body(5.0, -12.0, 6.0))
        )
        result = await weather_mod.get_weather_snapshot(_TORONTO_LAT, _TORONTO_LON)

    assert result == WeatherSnapshot(current_temp_c=5.0, overnight_low_c=-12.0, heat_warning=False)


@pytest.mark.unit
async def test_overnight_low_takes_minimum_across_today_and_tomorrow() -> None:
    """#110 review item 5: an evening run must not report a stale, warm
    THIS-MORNING minimum as tonight's low — tomorrow's (colder) forecast
    minimum wins when it's lower than today's."""
    with respx.mock() as respx_mock:
        respx_mock.get(_URL).mock(
            return_value=httpx.Response(200, json=_success_body(5.0, 2.0, 10.0, low_tomorrow=-9.0))
        )
        result = await weather_mod.get_weather_snapshot(_TORONTO_LAT, _TORONTO_LON)

    assert result is not None
    assert result.overnight_low_c == -9.0


@pytest.mark.unit
async def test_overnight_low_uses_today_when_it_is_colder_than_tomorrow() -> None:
    with respx.mock() as respx_mock:
        respx_mock.get(_URL).mock(
            return_value=httpx.Response(
                200, json=_success_body(5.0, -14.0, 10.0, low_tomorrow=-2.0)
            )
        )
        result = await weather_mod.get_weather_snapshot(_TORONTO_LAT, _TORONTO_LON)

    assert result is not None
    assert result.overnight_low_c == -14.0


@pytest.mark.unit
async def test_request_asks_open_meteo_for_two_forecast_days() -> None:
    with respx.mock() as respx_mock:
        route = respx_mock.get(_URL).mock(
            return_value=httpx.Response(
                200, json=_success_body(5.0, -12.0, 6.0, low_tomorrow=-10.0)
            )
        )
        await weather_mod.get_weather_snapshot(_TORONTO_LAT, _TORONTO_LON)

    assert route.calls.last is not None
    assert "forecast_days=2" in str(route.calls.last.request.url)


@pytest.mark.unit
async def test_heat_warning_flag_set_above_threshold() -> None:
    with respx.mock() as respx_mock:
        respx_mock.get(_URL).mock(
            return_value=httpx.Response(200, json=_success_body(28.0, 22.0, 33.0))
        )
        result = await weather_mod.get_weather_snapshot(_TORONTO_LAT, _TORONTO_LON)

    assert result is not None
    assert result.heat_warning is True


@pytest.mark.unit
async def test_heat_warning_flag_not_set_below_threshold() -> None:
    with respx.mock() as respx_mock:
        respx_mock.get(_URL).mock(
            return_value=httpx.Response(200, json=_success_body(20.0, 15.0, 25.0))
        )
        result = await weather_mod.get_weather_snapshot(_TORONTO_LAT, _TORONTO_LON)

    assert result is not None
    assert result.heat_warning is False


# ---------------------------------------------------------------------------
# Failure paths — timeout / non-2xx / malformed body -> None, never raises
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_timeout_returns_none() -> None:
    with respx.mock() as respx_mock:
        respx_mock.get(_URL).mock(side_effect=httpx.TimeoutException("timed out"))
        result = await weather_mod.get_weather_snapshot(_TORONTO_LAT, _TORONTO_LON)
    assert result is None


@pytest.mark.unit
async def test_connection_error_returns_none() -> None:
    with respx.mock() as respx_mock:
        respx_mock.get(_URL).mock(side_effect=httpx.ConnectError("boom"))
        result = await weather_mod.get_weather_snapshot(_TORONTO_LAT, _TORONTO_LON)
    assert result is None


@pytest.mark.unit
async def test_non_2xx_status_returns_none() -> None:
    with respx.mock() as respx_mock:
        respx_mock.get(_URL).mock(return_value=httpx.Response(500, json={"error": "boom"}))
        result = await weather_mod.get_weather_snapshot(_TORONTO_LAT, _TORONTO_LON)
    assert result is None


@pytest.mark.unit
async def test_malformed_body_returns_none() -> None:
    with respx.mock() as respx_mock:
        respx_mock.get(_URL).mock(return_value=httpx.Response(200, json={"unexpected": "shape"}))
        result = await weather_mod.get_weather_snapshot(_TORONTO_LAT, _TORONTO_LON)
    assert result is None


# ---------------------------------------------------------------------------
# Cache behaviour — TTL >= 30 min, keyed on rounded lat/lon
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_cache_hit_within_ttl_skips_network(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_time = [1_000.0]
    monkeypatch.setattr(weather_mod, "_now", lambda: fake_time[0])

    with respx.mock() as respx_mock:
        route = respx_mock.get(_URL).mock(
            return_value=httpx.Response(200, json=_success_body(5.0, -12.0, 6.0))
        )
        first = await weather_mod.get_weather_snapshot(_TORONTO_LAT, _TORONTO_LON)
        assert route.call_count == 1

        # 29 minutes later -- still within the >= 30 min TTL.
        fake_time[0] += 29 * 60
        second = await weather_mod.get_weather_snapshot(_TORONTO_LAT, _TORONTO_LON)
        assert route.call_count == 1  # no new network call
        assert second == first


@pytest.mark.unit
async def test_cache_expires_after_ttl_refetches(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_time = [2_000.0]
    monkeypatch.setattr(weather_mod, "_now", lambda: fake_time[0])

    with respx.mock() as respx_mock:
        route = respx_mock.get(_URL).mock(
            return_value=httpx.Response(200, json=_success_body(5.0, -12.0, 6.0))
        )
        await weather_mod.get_weather_snapshot(_TORONTO_LAT, _TORONTO_LON)
        assert route.call_count == 1

        # Exactly 30 minutes later -- TTL uses a strict `<` comparison, so
        # this is no longer a cache hit (the AC only requires >= 30 min of
        # freshness, not more).
        fake_time[0] += 30 * 60
        respx_mock.get(_URL).mock(
            return_value=httpx.Response(200, json=_success_body(6.0, -11.0, 7.0))
        )
        await weather_mod.get_weather_snapshot(_TORONTO_LAT, _TORONTO_LON)
        assert route.call_count == 2


@pytest.mark.unit
async def test_cache_key_rounds_nearby_coordinates_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_time = [3_000.0]
    monkeypatch.setattr(weather_mod, "_now", lambda: fake_time[0])

    with respx.mock() as respx_mock:
        route = respx_mock.get(_URL).mock(
            return_value=httpx.Response(200, json=_success_body(5.0, -12.0, 6.0))
        )
        await weather_mod.get_weather_snapshot(_TORONTO_LAT, _TORONTO_LON)
        # A coordinate that rounds to the same 2-decimal key.
        await weather_mod.get_weather_snapshot(_TORONTO_LAT + 0.0001, _TORONTO_LON - 0.0001)
        assert route.call_count == 1


@pytest.mark.unit
async def test_cache_key_distinguishes_far_apart_coordinates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_time = [4_000.0]
    monkeypatch.setattr(weather_mod, "_now", lambda: fake_time[0])

    with respx.mock() as respx_mock:
        route = respx_mock.get(_URL).mock(
            return_value=httpx.Response(200, json=_success_body(5.0, -12.0, 6.0))
        )
        await weather_mod.get_weather_snapshot(_TORONTO_LAT, _TORONTO_LON)
        await weather_mod.get_weather_snapshot(45.0, -75.0)  # Ottawa — different key
        assert route.call_count == 2


@pytest.mark.unit
async def test_failed_fetch_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider failure must never poison the cache with a `None` — the
    next call should retry, not silently stay unavailable for the TTL."""
    fake_time = [5_000.0]
    monkeypatch.setattr(weather_mod, "_now", lambda: fake_time[0])

    with respx.mock() as respx_mock:
        route = respx_mock.get(_URL).mock(side_effect=httpx.TimeoutException("timed out"))
        first = await weather_mod.get_weather_snapshot(_TORONTO_LAT, _TORONTO_LON)
        assert first is None
        assert route.call_count == 1

        respx_mock.get(_URL).mock(
            return_value=httpx.Response(200, json=_success_body(5.0, -12.0, 6.0))
        )
        second = await weather_mod.get_weather_snapshot(_TORONTO_LAT, _TORONTO_LON)
        assert second is not None
        assert route.call_count == 2
