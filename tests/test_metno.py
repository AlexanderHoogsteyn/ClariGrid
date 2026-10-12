"""Tests for the MET Norway Locationforecast provider."""

from __future__ import annotations

import pandas as pd
import pytest

import clarigrid as cg
import clarigrid.core.cache as cache
from clarigrid.core.cache import CacheManager
from clarigrid.core.exceptions import ProviderError
from clarigrid.core.normalise import EnergyFrame
from clarigrid.providers import metno as module
from clarigrid.providers.metno import MetNoProvider, _parse_response, _parse_zone
from tests.conftest import assert_valid_weather_df, median_interval

PROVIDER = "metno"
ZONE = "50.850346,4.351721"


@pytest.fixture
def forecast_payload() -> dict:
    def step(timestamp: str, temperature: float, wind: float, precipitation: float | None):
        data = {
            "instant": {
                "details": {
                    "air_pressure_at_sea_level": 1019.1,
                    "air_temperature": temperature,
                    "cloud_area_fraction": 29.7,
                    "relative_humidity": 93.7,
                    "wind_from_direction": 166.6,
                    "wind_speed": wind,
                }
            }
        }
        if precipitation is not None:
            data["next_1_hours"] = {
                "summary": {"symbol_code": "fair_day"},
                "details": {"precipitation_amount": precipitation},
            }
        return {"time": timestamp, "data": data}

    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [4.3517, 50.8503, 35]},
        "properties": {
            "meta": {
                "updated_at": "2026-10-12T07:18:14Z",
                "units": {
                    "air_pressure_at_sea_level": "hPa",
                    "air_temperature": "celsius",
                    "cloud_area_fraction": "%",
                    "precipitation_amount": "mm",
                    "relative_humidity": "%",
                    "wind_from_direction": "degrees",
                    "wind_speed": "m/s",
                },
            },
            "timeseries": [
                step("2026-10-12T07:00:00Z", 8.4, 2.4, 0.0),
                step("2026-10-12T08:00:00Z", 10.4, 2.1, 0.2),
                step("2026-10-12T09:00:00Z", 12.6, 1.7, None),
            ],
        },
    }


def test_metno_registered_and_declares_weather_capability():
    assert PROVIDER in cg.list_providers()
    provider = MetNoProvider()
    assert provider.capabilities() == {"weather"}
    assert provider.zones() == {"*"}


def test_parse_zone_validates_and_rounds_coordinates():
    assert _parse_zone(ZONE) == (50.8503, 4.3517)
    with pytest.raises(ValueError, match="Latitude"):
        _parse_zone("91,0")
    with pytest.raises(ValueError, match="lat,lon"):
        _parse_zone("Brussels")


def test_parser_normalises_fields_units_and_metadata(forecast_payload):
    frame = _parse_response(
        forecast_payload,
        ["air_temperature", "wind_speed", "precipitation_amount"],
        "2026-10-12",
        "2026-10-12",
    )

    assert_valid_weather_df(
        frame,
        expected_cols=["temperature_c", "wind_speed_ms", "precipitation_mm"],
        min_rows=3,
    )
    assert median_interval(frame) == pd.Timedelta("1h")
    assert frame.iloc[0]["temperature_c"] == pytest.approx(8.4)
    assert frame.iloc[1]["precipitation_mm"] == pytest.approx(0.2)
    assert pd.isna(frame.iloc[2]["precipitation_mm"])
    assert frame.attrs["license"] == "NLOD 2.0 / CC BY 4.0"
    assert frame.attrs["attribution"] == "Data from MET Norway"
    assert frame.attrs["data_updated_at"] == "2026-10-12T07:18:14Z"
    assert frame.attrs["rate_limit_requests_per_second"] == 20
    assert frame.attrs["query_limit"] is None
    assert frame.attrs["units"]["wind_speed_ms"] == "m/s"


def test_parser_clips_requested_utc_window(forecast_payload):
    frame = _parse_response(
        forecast_payload,
        ["air_temperature"],
        "2026-10-12T08:00:00Z",
        "2026-10-12T09:00:00Z",
    )
    assert list(frame.index) == [pd.Timestamp("2026-10-12T08:00:00Z")]


def test_parser_rejects_changed_upstream_unit(forecast_payload):
    forecast_payload["properties"]["meta"]["units"]["wind_speed"] = "km/h"
    with pytest.raises(ProviderError, match="unexpected unit"):
        _parse_response(
            forecast_payload,
            ["wind_speed"],
            "2026-10-12",
            "2026-10-12",
        )


def test_parser_rejects_changed_schema():
    with pytest.raises(ProviderError, match="GeoJSON"):
        _parse_response({}, ["air_temperature"], "2026-10-12", "2026-10-12")


def test_provider_requests_compact_forecast_with_altitude(monkeypatch, forecast_payload):
    calls: list[tuple[str, dict, dict | None]] = []

    def fake_get_json(url, params=None, headers=None, **kwargs):
        calls.append((url, params, headers))
        return forecast_payload

    monkeypatch.setattr(module, "get_json", fake_get_json)
    frame = MetNoProvider().get_weather(
        ZONE,
        "2026-10-12",
        "2026-10-12",
        variables=["temperature_c", "wind_speed"],
        altitude=35,
    )

    assert list(frame.columns) == ["temperature_c", "wind_speed_ms"]
    assert calls == [
        (
            "https://api.met.no/weatherapi/locationforecast/2.0/compact",
            {"lat": 50.8503, "lon": 4.3517, "altitude": 35},
            {"Accept": "application/geo+json"},
        )
    ]
    with pytest.raises(ValueError, match="whole number"):
        MetNoProvider().get_weather(ZONE, "2026-10-12", "2026-10-12", altitude=35.5)


@pytest.mark.skipif(not cache._PARQUET_OK, reason="pyarrow not installed")
def test_public_api_cache_and_timezone(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "_manager", CacheManager(tmp_path))
    calls = 0

    def fake_weather(self, zone, start, end, **kwargs):
        nonlocal calls
        calls += 1
        return EnergyFrame(
            {"temperature_c": [10.0]},
            index=pd.DatetimeIndex(["2026-10-12T08:00:00Z"], name="utc_time"),
        )

    monkeypatch.setattr(MetNoProvider, "get_weather", fake_weather)
    cg.reset()
    cg.connect(PROVIDER)
    try:
        first = cg.get_weather(ZONE, "2026-10-12", "2026-10-12", source=PROVIDER)
        second = cg.get_weather(ZONE, "2026-10-12", "2026-10-12", source=PROVIDER)
        assert calls == 1
        assert first.attrs["provider"] == PROVIDER
        assert first.attrs["dataset"] == "weather"
        assert first.attrs["zone"] == ZONE
        assert second.iloc[0]["temperature_c"] == 10.0

        cg.set_timezone("Europe/Brussels")
        localized = cg.get_weather(
            ZONE, "2026-10-12", "2026-10-12", source=PROVIDER
        )
        assert str(localized.index.tz) == "Europe/Brussels"
        assert calls == 1
    finally:
        cg.reset()


def test_public_api_no_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "_manager", CacheManager(tmp_path))
    calls = 0

    def fake_weather(self, zone, start, end, **kwargs):
        nonlocal calls
        calls += 1
        return EnergyFrame(
            {"temperature_c": [10.0]},
            index=pd.DatetimeIndex(["2026-10-12T08:00:00Z"], name="utc_time"),
        )

    monkeypatch.setattr(MetNoProvider, "get_weather", fake_weather)
    for _ in range(2):
        frame = cg.get_weather(
            ZONE,
            "2026-10-12",
            "2026-10-12",
            source=PROVIDER,
            use_cache=False,
        )
    assert calls == 2
    assert frame.attrs["provider"] == PROVIDER
    assert not list(tmp_path.iterdir())


def test_public_api_routes_connected_provider(monkeypatch):
    def fake_weather(self, zone, start, end, **kwargs):
        return EnergyFrame(
            {"temperature_c": [10.0]},
            index=pd.DatetimeIndex(["2026-10-12T08:00:00Z"], name="utc_time"),
        )

    monkeypatch.setattr(MetNoProvider, "get_weather", fake_weather)
    cg.reset()
    cg.connect(PROVIDER)
    try:
        frame = cg.get_weather(ZONE, "2026-10-12", "2026-10-12", use_cache=False)
        assert frame.attrs["provider"] == PROVIDER
    finally:
        cg.reset()


@pytest.mark.live
def test_metno_live_smoke():
    today = pd.Timestamp.now(tz="UTC").normalize()
    frame = cg.get_weather(
        ZONE,
        today,
        today + pd.Timedelta(days=1),
        source=PROVIDER,
        variables=["air_temperature", "wind_speed", "relative_humidity"],
        use_cache=False,
    )
    assert_valid_weather_df(
        frame,
        expected_cols=["temperature_c", "wind_speed_ms", "humidity_pct"],
        min_rows=12,
    )
    assert median_interval(frame) == pd.Timedelta("1h")
