"""Tests for NOAA NCEI daily U.S. station observations."""

from __future__ import annotations

import pandas as pd
import pytest

import clarigrid as cg
import clarigrid.core.cache as cache
from clarigrid.core.cache import CacheManager
from clarigrid.core.exceptions import ProviderError
from clarigrid.core.normalise import EnergyFrame
from clarigrid.core.router import ZoneRouter
from clarigrid.providers import ncei as module
from clarigrid.providers.ncei import NCEIProvider, _parse_response, _parse_zone
from tests.conftest import assert_valid_weather_df, median_interval

PROVIDER = "ncei"
ZONE = "USW00094728"


@pytest.fixture
def daily_payload() -> list[dict[str, str]]:
    base = {
        "STATION": ZONE,
        "NAME": "NY CITY CENTRAL PARK, NY US",
        "LATITUDE": "40.77898",
        "LONGITUDE": "-73.96925",
        "ELEVATION": "42.7",
        "PRCP": "0.0",
        "AWND": "3.7",
        "SNOW": "0.0",
        "SNWD": "0.0",
    }
    return [
        {**base, "DATE": "2025-01-01", "TMAX": "10.6", "TMIN": "3.9"},
        {**base, "DATE": "2025-01-02", "TMAX": "6.1", "TMIN": "0.6"},
    ]


def test_ncei_registered_and_routes_weather_stations():
    assert PROVIDER in cg.list_providers()
    provider = NCEIProvider()
    assert provider.capabilities() == {"weather"}
    assert provider.zones() == {"*"}

    router = ZoneRouter()
    router.register_coverage(PROVIDER, provider.capability_zones())
    assert router.resolve(ZONE, "weather") == PROVIDER


def test_station_and_variable_validation():
    assert _parse_zone("usw00094728") == ZONE
    with pytest.raises(ValueError, match="U.S. GHCN station ID"):
        _parse_zone("94728")
    with pytest.raises(ValueError, match="Unsupported NCEI variable"):
        NCEIProvider().get_weather(ZONE, "2025-01-01", "2025-01-02", variables=["UV"])


def test_parser_normalises_metric_daily_weather(daily_payload):
    variables = ["TMAX", "TMIN", "PRCP", "AWND", "SNOW", "SNWD"]
    frame = _parse_response(daily_payload, variables, ZONE)

    assert_valid_weather_df(
        frame,
        expected_cols=[
            "temperature_max_c",
            "temperature_min_c",
            "precipitation_mm",
            "wind_speed_ms",
            "snowfall_mm",
            "snow_depth_mm",
        ],
        min_rows=2,
    )
    assert median_interval(frame) == pd.Timedelta(days=1)
    assert frame.iloc[0]["temperature_max_c"] == pytest.approx(10.6)
    assert frame.attrs["station_name"] == "NY CITY CENTRAL PARK, NY US"
    assert frame.attrs["license"] == "U.S. public domain"
    assert frame.attrs["api_version"] == "v1"
    assert frame.attrs["query_limit"] is None
    assert frame.attrs["rate_limit"] is None
    assert frame.attrs["units"]["wind_speed_ms"] == "m/s"


def test_parser_rejects_changed_schema_station_duplicates_and_values(daily_payload):
    row = {
        key: value
        for key, value in daily_payload[0].items()
        if key in module._METADATA_FIELDS | {"TMAX"}
    }
    with pytest.raises(ProviderError, match="unexpected station"):
        _parse_response([{**row, "STATION": "USW00094729"}], ["TMAX"], ZONE)
    with pytest.raises(ProviderError, match="duplicate"):
        _parse_response([row, row], ["TMAX"], ZONE)
    with pytest.raises(ProviderError, match="unexpected daily-summaries"):
        _parse_response([{**row, "EXTRA": "1"}], ["TMAX"], ZONE)
    with pytest.raises(ProviderError, match="invalid TMAX"):
        _parse_response([{**row, "TMAX": "hot"}], ["TMAX"], ZONE)


def test_provider_requests_bounded_metric_json(monkeypatch, daily_payload):
    calls: list[tuple[str, dict]] = []

    def fake_get_json(url, params):
        calls.append((url, dict(params)))
        row = {
            key: value
            for key, value in daily_payload[0].items()
            if key in module._METADATA_FIELDS | set(params["dataTypes"].split(","))
        }
        row["DATE"] = params["startDate"]
        return [row]

    monkeypatch.setattr(module, "get_json", fake_get_json)
    frame = NCEIProvider().get_weather(
        ZONE,
        "2024-12-31",
        "2025-01-02",
        variables=["temperature_max_c", "PRCP"],
    )

    assert len(calls) == 2
    assert calls[0][0] == module._URL
    assert calls[0][1]["startDate"] == "2024-12-31"
    assert calls[0][1]["endDate"] == "2024-12-31"
    assert calls[1][1]["startDate"] == "2025-01-01"
    assert calls[1][1]["endDate"] == "2025-01-02"
    assert calls[1][1]["dataTypes"] == "TMAX,PRCP"
    assert calls[1][1]["units"] == "metric"
    assert calls[1][1]["includeAttributes"] == "false"
    assert list(frame.columns) == ["temperature_max_c", "precipitation_mm"]
    assert frame.attrs["license"] == "U.S. public domain"


@pytest.mark.skipif(not cache._PARQUET_OK, reason="pyarrow not installed")
def test_public_api_cache_timezone_and_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "_manager", CacheManager(tmp_path))
    calls = 0

    def fake_weather(self, zone, start, end, **kwargs):
        nonlocal calls
        calls += 1
        frame = EnergyFrame(
            {"temperature_max_c": [10.6]},
            index=pd.DatetimeIndex(["2025-01-01T00:00:00Z"], name="utc_time"),
        )
        frame.attrs["license"] = "U.S. public domain"
        return frame

    monkeypatch.setattr(NCEIProvider, "get_weather", fake_weather)
    cg.reset()
    cg.connect(PROVIDER)
    try:
        first = cg.get_weather(ZONE, "2025-01-01", "2025-01-01", variables=["TMAX"])
        cg.set_timezone("US/Eastern")
        second = cg.get_weather(ZONE, "2025-01-01", "2025-01-01", variables=["TMAX"])
        assert calls == 1
        assert first.attrs["provider"] == PROVIDER
        assert first.attrs["dataset"] == "weather"
        assert first.attrs["zone"] == ZONE
        assert first.attrs["license"] == "U.S. public domain"
        assert str(second.index.tz) == "US/Eastern"
    finally:
        cg.reset()


def test_public_api_no_cache_and_connected_routing(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "_manager", CacheManager(tmp_path))
    calls = 0

    def fake_weather(self, zone, start, end, **kwargs):
        nonlocal calls
        calls += 1
        return EnergyFrame(
            {"temperature_max_c": [10.6]},
            index=pd.DatetimeIndex(["2025-01-01T00:00:00Z"], name="utc_time"),
        )

    monkeypatch.setattr(NCEIProvider, "get_weather", fake_weather)
    cg.reset()
    cg.connect(PROVIDER)
    try:
        for _ in range(2):
            frame = cg.get_weather(ZONE, "2025-01-01", "2025-01-01", use_cache=False)
        assert calls == 2
        assert frame.attrs["provider"] == PROVIDER
        assert not list(tmp_path.iterdir())
    finally:
        cg.reset()


@pytest.mark.live
def test_ncei_live_smoke():
    frame = cg.get_weather(
        ZONE,
        "2025-01-01",
        "2025-01-03",
        source=PROVIDER,
        variables=["TMAX", "TMIN", "PRCP"],
        use_cache=False,
    )
    assert_valid_weather_df(
        frame,
        expected_cols=["temperature_max_c", "temperature_min_c", "precipitation_mm"],
        min_rows=3,
    )
    assert median_interval(frame) == pd.Timedelta(days=1)
