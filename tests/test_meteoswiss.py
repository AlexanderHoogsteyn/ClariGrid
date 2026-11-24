"""Tests for MeteoSwiss SwissMetNet hourly observations."""

from __future__ import annotations

import pandas as pd
import pytest

import clarigrid as cg
import clarigrid.core.cache as cache
from clarigrid.core.cache import CacheManager
from clarigrid.core.exceptions import ProviderError
from clarigrid.core.normalise import EnergyFrame
from clarigrid.core.router import ZoneRouter
from clarigrid.providers import meteoswiss as module
from clarigrid.providers.meteoswiss import (
    MeteoSwissProvider,
    _parse_observations,
    _parse_units,
    _parse_zone,
    _select_assets,
)
from tests.conftest import assert_valid_weather_df, median_interval

PROVIDER = "meteoswiss"
ZONE = "ABO"


def _metadata(unit: str = "°C") -> bytes:
    return (
        "parameter_shortname;parameter_granularity;parameter_unit\n"
        f"tre200h0;H;{unit}\n"
        "ure200h0;H;%\n"
        "pp0qnhh0;H;hPa\n"
        "fkl010h0;H;m/s\n"
        "dkl010h0;H;°\n"
        "fkl010h1;H;m/s\n"
        "rre150h0;H;mm\n"
        "sre000h0;H;min\n"
    ).encode("cp1252")


def _observations(*, station: str = ZONE, duplicate: bool = False, bad: str = "6.3") -> bytes:
    rows = [
        f"{station};23.11.2026 12:00;{bad};87;1017.2;3.2;225;5.5;0.0;12",
        f"{station};23.11.2026 13:00;5.8;85;1016.8;3.4;230;6.1;;18",
    ]
    if duplicate:
        rows.append(rows[0])
    return (
        "station_abbr;reference_timestamp;tre200h0;ure200h0;pp0qnhh0;"
        "fkl010h0;dkl010h0;fkl010h1;rre150h0;sre000h0\n"
        + "\n".join(rows)
        + "\n"
    ).encode("cp1252")


def _asset(name: str) -> dict[str, str]:
    return {
        "type": "text/csv",
        "href": f"https://data.geo.admin.ch/{module._COLLECTION}/abo/{name}",
    }


def _item() -> dict:
    names = [
        "ogd-smn_abo_h_historical_2020-2029.csv",
        "ogd-smn_abo_h_recent.csv",
        "ogd-smn_abo_h_now.csv",
    ]
    return {
        "id": "abo",
        "collection": module._COLLECTION,
        "type": "Feature",
        "stac_version": "1.0.0",
        "properties": {
            "title": "Adelboden (ABO)",
            "updated": "2026-11-24T08:48:06Z",
        },
        "geometry": {"type": "Point", "coordinates": [7.560703, 46.491703]},
        "assets": {name: _asset(name) for name in names},
    }


def test_meteoswiss_registered_and_routes_weather_stations():
    assert PROVIDER in cg.list_providers()
    provider = MeteoSwissProvider()
    assert provider.capabilities() == {"weather"}
    assert provider.zones() == {"*"}

    router = ZoneRouter()
    router.register_coverage(PROVIDER, provider.capability_zones())
    assert router.resolve(ZONE, "weather") == PROVIDER


def test_station_variable_and_unit_validation():
    assert _parse_zone("abo") == ZONE
    _parse_units(_metadata(), ["tre200h0", "fkl010h0"])

    with pytest.raises(ValueError, match="three-letter SwissMetNet"):
        _parse_zone("Adelboden")
    with pytest.raises(ValueError, match="Unsupported MeteoSwiss variable"):
        MeteoSwissProvider().get_weather(ZONE, "2026-11-23", "2026-11-24", variables=["uv"])
    with pytest.raises(ProviderError, match="unexpected metadata"):
        _parse_units(_metadata("°F"), ["tre200h0"])


def test_parser_normalises_hourly_observations_and_missing_values():
    variables = [
        "tre200h0",
        "ure200h0",
        "pp0qnhh0",
        "fkl010h0",
        "dkl010h0",
        "fkl010h1",
        "rre150h0",
        "sre000h0",
    ]
    frame = _parse_observations(_observations(), variables, ZONE)

    assert_valid_weather_df(
        frame,
        expected_cols=[
            "temperature_c",
            "humidity_pct",
            "pressure_hpa",
            "wind_speed_ms",
            "wind_direction_deg",
            "wind_gust_ms",
            "precipitation_mm",
            "sunshine_duration_min",
        ],
        min_rows=2,
    )
    assert median_interval(frame) == pd.Timedelta(hours=1)
    assert frame.iloc[0]["temperature_c"] == pytest.approx(6.3)
    assert pd.isna(frame.iloc[1]["precipitation_mm"])


def test_parser_rejects_station_duplicates_schema_and_values():
    variables = ["tre200h0", "ure200h0"]
    with pytest.raises(ProviderError, match="unexpected station"):
        _parse_observations(_observations(station="ALT"), variables, ZONE)
    with pytest.raises(ProviderError, match="duplicate hourly"):
        _parse_observations(_observations(duplicate=True), variables, ZONE)
    with pytest.raises(ProviderError, match="unexpected hourly observation schema"):
        _parse_observations(b"station_abbr;reference_timestamp\n", variables, ZONE)
    with pytest.raises(ProviderError, match="invalid tre200h0"):
        _parse_observations(_observations(bad="warm"), variables, ZONE)


def test_asset_selection_uses_history_recent_and_now_without_pagination():
    item = _item()
    assets = item["assets"]
    updated = pd.Timestamp(item["properties"]["updated"])

    historical = _select_assets(
        assets,
        ZONE,
        pd.Timestamp("2025-01-01T00:00:00Z"),
        pd.Timestamp("2025-01-02T00:00:00Z"),
        updated,
    )
    current = _select_assets(
        assets,
        ZONE,
        pd.Timestamp("2026-11-23T12:00:00Z"),
        pd.Timestamp("2026-11-24T09:00:00Z"),
        updated,
    )

    assert [kind for kind, _ in historical] == ["historical"]
    assert [kind for kind, _ in current] == ["recent", "now"]


def test_provider_discovers_station_asset_and_attaches_metadata(monkeypatch):
    calls: list[str] = []

    def fake_get_json(url):
        calls.append(url)
        return _item()

    def fake_get_bytes(url):
        calls.append(url)
        return _metadata() if url == module._PARAMETERS_URL else _observations()

    monkeypatch.setattr(module, "get_json", fake_get_json)
    monkeypatch.setattr(module, "get_bytes", fake_get_bytes)
    frame = MeteoSwissProvider().get_weather(
        ZONE,
        "2026-11-23T12:00:00Z",
        "2026-11-23T14:00:00Z",
        variables=["temperature_c", "ure200h0", "rre150h0"],
    )

    assert calls[0] == f"{module._STAC_URL}/items/abo"
    assert calls[1] == module._PARAMETERS_URL
    assert len(calls) == 4
    assert list(frame.columns) == ["temperature_c", "humidity_pct", "precipitation_mm"]
    assert len(frame) == 2
    assert frame.attrs["station_name"] == "Adelboden"
    assert frame.attrs["license"] == "CC BY 4.0"
    assert frame.attrs["attribution"] == "Source: MeteoSwiss"
    assert frame.attrs["api_version"] == "OGC STAC API v1; STAC 1.0.0"
    assert frame.attrs["query_limit"] is None
    assert frame.attrs["rate_limit"] is None
    assert frame.attrs["units"]["precipitation_mm"] == "mm/1h"


@pytest.mark.skipif(not cache._PARQUET_OK, reason="pyarrow not installed")
def test_public_api_cache_timezone_and_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "_manager", CacheManager(tmp_path))
    calls = 0

    def fake_weather(self, zone, start, end, **kwargs):
        nonlocal calls
        calls += 1
        frame = EnergyFrame(
            {"temperature_c": [6.3]},
            index=pd.DatetimeIndex(["2026-11-23T12:00:00Z"], name="utc_time"),
        )
        frame.attrs["license"] = "CC BY 4.0"
        return frame

    monkeypatch.setattr(MeteoSwissProvider, "get_weather", fake_weather)
    cg.reset()
    cg.connect(PROVIDER)
    try:
        first = cg.get_weather(ZONE, "2026-11-23", "2026-11-23", variables=["tre200h0"])
        cg.set_timezone("Europe/Zurich")
        second = cg.get_weather(ZONE, "2026-11-23", "2026-11-23", variables=["tre200h0"])
        assert calls == 1
        assert first.attrs["provider"] == PROVIDER
        assert first.attrs["dataset"] == "weather"
        assert first.attrs["zone"] == ZONE
        assert first.attrs["license"] == "CC BY 4.0"
        assert str(second.index.tz) == "Europe/Zurich"
    finally:
        cg.reset()


def test_public_api_no_cache_and_connected_routing(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "_manager", CacheManager(tmp_path))
    calls = 0

    def fake_weather(self, zone, start, end, **kwargs):
        nonlocal calls
        calls += 1
        return EnergyFrame(
            {"temperature_c": [6.3]},
            index=pd.DatetimeIndex(["2026-11-23T12:00:00Z"], name="utc_time"),
        )

    monkeypatch.setattr(MeteoSwissProvider, "get_weather", fake_weather)
    cg.reset()
    cg.connect(PROVIDER)
    try:
        for _ in range(2):
            frame = cg.get_weather(
                ZONE, "2026-11-23", "2026-11-23", source=PROVIDER, use_cache=False
            )
        assert calls == 2
        assert frame.attrs["provider"] == PROVIDER
        assert not list(tmp_path.iterdir())
    finally:
        cg.reset()


@pytest.mark.live
def test_meteoswiss_live_smoke():
    end = pd.Timestamp.now(tz="UTC").floor("h")
    frame = cg.get_weather(
        ZONE,
        end - pd.Timedelta(hours=4),
        end - pd.Timedelta(hours=1),
        source=PROVIDER,
        variables=["tre200h0", "ure200h0", "fkl010h0", "rre150h0"],
        use_cache=False,
    )
    assert_valid_weather_df(
        frame,
        expected_cols=["temperature_c", "humidity_pct", "wind_speed_ms", "precipitation_mm"],
        min_rows=2,
    )
    assert median_interval(frame) == pd.Timedelta(hours=1)
