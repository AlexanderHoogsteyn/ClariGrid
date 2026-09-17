"""Tests for the NOAA National Weather Service provider."""

from __future__ import annotations

import pandas as pd
import pytest

import clarigrid as cg
import clarigrid.core.cache as cache
from clarigrid.core.cache import CacheManager
from clarigrid.core.exceptions import ProviderError
from clarigrid.core.normalise import EnergyFrame
from clarigrid.providers import nws as module
from clarigrid.providers.nws import NWSProvider, _parse_response, _parse_zone
from tests.conftest import assert_valid_weather_df, median_interval

PROVIDER = "nws"
ZONE = "39.7456,-97.0892"


@pytest.fixture
def point_payload() -> dict:
    return {
        "geometry": {"coordinates": [-97.0892, 39.7456]},
        "properties": {
            "forecastGridData": "https://api.weather.gov/gridpoints/TOP/32,81",
            "gridId": "TOP",
            "timeZone": "America/Chicago",
        },
    }


@pytest.fixture
def grid_payload() -> dict:
    def field(unit: str, first: float, second: float) -> dict:
        return {
            "uom": unit,
            "values": [
                {"validTime": "2026-09-17T00:00:00+00:00/PT2H", "value": first},
                {"validTime": "2026-09-17T02:00:00+00:00/PT1H", "value": second},
            ],
        }

    return {
        "properties": {
            "updateTime": "2026-09-16T23:00:00+00:00",
            "temperature": field("wmoUnit:degC", 20, 21),
            "dewpoint": field("wmoUnit:degC", 10, 11),
            "relativeHumidity": field("wmoUnit:percent", 60, 65),
            "probabilityOfPrecipitation": field("wmoUnit:percent", 5, 10),
            "windSpeed": field("wmoUnit:km_h-1", 18, 36),
            "windDirection": field("wmoUnit:degree_(angle)", 180, 190),
            "skyCover": field("wmoUnit:percent", 25, 50),
        }
    }


def test_nws_registered_and_declares_weather_capability():
    assert PROVIDER in cg.list_providers()
    provider = NWSProvider()
    assert provider.capabilities() == {"weather"}
    assert provider.zones() == {"*"}


def test_parse_zone_validates_coordinates():
    assert _parse_zone(ZONE) == (39.7456, -97.0892)
    with pytest.raises(ValueError, match="Latitude"):
        _parse_zone("91,0")
    with pytest.raises(ValueError, match="lat,lon"):
        _parse_zone("Kansas")


def test_parser_expands_intervals_and_normalises_units(grid_payload, point_payload):
    frame = _parse_response(
        grid_payload,
        ["temperature", "windSpeed", "relativeHumidity"],
        "2026-09-17",
        "2026-09-17",
        point=point_payload,
    )

    assert_valid_weather_df(
        frame,
        expected_cols=["temperature_c", "wind_speed_ms", "humidity_pct"],
        min_rows=3,
    )
    assert median_interval(frame) == pd.Timedelta("1h")
    assert frame.iloc[0]["wind_speed_ms"] == pytest.approx(5.0)
    assert frame.iloc[2]["wind_speed_ms"] == pytest.approx(10.0)
    assert frame.attrs["license"] == "U.S. public domain unless otherwise noted"
    assert frame.attrs["forecast_office"] == "TOP"
    assert frame.attrs["units"]["wind_speed_ms"] == "m/s"


def test_parser_rejects_changed_upstream_units(grid_payload):
    grid_payload["properties"]["temperature"]["uom"] = "wmoUnit:degF"
    with pytest.raises(ProviderError, match="unexpected unit"):
        _parse_response(grid_payload, ["temperature"], "2026-09-17", "2026-09-17")


def test_empty_forecast_keeps_utc_index(grid_payload):
    grid_payload["properties"]["temperature"]["values"] = []
    frame = _parse_response(grid_payload, ["temperature"], "2030-01-01", "2030-01-01")
    assert frame.empty
    assert str(frame.index.tz) == "UTC"


def test_provider_discovers_grid_and_sends_required_headers(
    monkeypatch, grid_payload, point_payload
):
    calls: list[tuple[str, dict | None]] = []

    def fake_get_json(url, params=None, headers=None, **kwargs):
        calls.append((url, headers))
        return point_payload if "/points/" in url else grid_payload

    monkeypatch.setattr(module, "get_json", fake_get_json)
    frame = NWSProvider().get_weather(
        ZONE,
        "2026-09-17",
        "2026-09-17",
        variables=["temperature_c", "windSpeed"],
    )

    assert list(frame.columns) == ["temperature_c", "wind_speed_ms"]
    assert calls[0][0].endswith("/points/39.7456,-97.0892")
    assert calls[1][0] == point_payload["properties"]["forecastGridData"]
    assert all(headers == {"Accept": "application/geo+json"} for _, headers in calls)


def test_public_api_cache_and_no_cache(monkeypatch, tmp_path):
    manager = CacheManager(tmp_path)
    monkeypatch.setattr(cache, "_manager", manager)
    monkeypatch.setattr(cache, "_PARQUET_OK", True)
    calls = 0

    def fake_weather(self, zone, start, end, **kwargs):
        nonlocal calls
        calls += 1
        frame = EnergyFrame(
            {"temperature_c": [20.0]},
            index=pd.DatetimeIndex(["2026-09-17T00:00:00Z"], name="utc_time"),
        )
        return frame

    monkeypatch.setattr(NWSProvider, "get_weather", fake_weather)
    cg.reset()
    cg.connect(PROVIDER)
    try:
        first = cg.get_weather(ZONE, "2026-09-17", "2026-09-17", source=PROVIDER)
        second = cg.get_weather(ZONE, "2026-09-17", "2026-09-17", source=PROVIDER)
        assert calls == 1
        assert first.attrs["provider"] == PROVIDER
        assert first.attrs["dataset"] == "weather"
        assert first.attrs["zone"] == ZONE
        assert second.iloc[0]["temperature_c"] == 20.0

        cg.set_timezone("America/Chicago")
        localized = cg.get_weather(
            ZONE, "2026-09-17", "2026-09-17", source=PROVIDER
        )
        assert str(localized.index.tz) == "America/Chicago"
        assert calls == 1

        cached_files = set(tmp_path.glob("*"))
        cg.get_weather(
            ZONE,
            "2026-09-18",
            "2026-09-18",
            source=PROVIDER,
            use_cache=False,
        )
        cg.get_weather(
            ZONE,
            "2026-09-18",
            "2026-09-18",
            source=PROVIDER,
            use_cache=False,
        )
        assert calls == 3
        assert set(tmp_path.glob("*")) == cached_files
    finally:
        cg.reset()


@pytest.mark.live
def test_nws_live_smoke():
    today = pd.Timestamp.now(tz="UTC").normalize()
    frame = cg.get_weather(
        ZONE,
        today,
        today + pd.Timedelta(days=1),
        source=PROVIDER,
        variables=["temperature", "windSpeed", "relativeHumidity"],
        use_cache=False,
    )
    assert_valid_weather_df(
        frame,
        expected_cols=["temperature_c", "wind_speed_ms", "humidity_pct"],
        min_rows=12,
    )
    assert median_interval(frame) == pd.Timedelta("1h")
