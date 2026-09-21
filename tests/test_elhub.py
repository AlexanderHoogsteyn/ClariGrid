"""Tests for the Elhub Energy Data provider."""

from __future__ import annotations

import pandas as pd
import pytest

import clarigrid as cg
import clarigrid.core.cache as cache
from clarigrid.core.cache import CacheManager
from clarigrid.core.exceptions import ProviderError
from clarigrid.core.normalise import EnergyFrame
from clarigrid.core.router import ZoneRouter
from clarigrid.providers import elhub as module
from clarigrid.providers.elhub import ElhubProvider, _parse_response
from tests.conftest import assert_valid_df, median_interval

PROVIDER = "elhub"
ZONE = "NO1"


def _payload(kind: str = "consumption", *, hours: int = 1) -> dict:
    group_key = f"{kind}Group"
    rows = [
        {
            "startTime": "2026-09-01T00:00:00+02:00",
            "endTime": f"2026-09-01T0{hours}:00:00+02:00",
            group_key: "household" if kind == "consumption" else "hydro",
            "quantityKwh": 2_000 * hours,
        },
        {
            "startTime": "2026-09-01T00:00:00+02:00",
            "endTime": f"2026-09-01T0{hours}:00:00+02:00",
            group_key: "tertiary" if kind == "consumption" else "wind",
            "quantityKwh": 1_000 * hours,
        },
    ]
    attribute = (
        "consumptionPerGroupMbaHour"
        if kind == "consumption"
        else "productionPerGroupMbaHour"
    )
    return {
        "meta": {"lastUpdated": "2026-09-15T17:34:12+02:00"},
        "data": [{"attributes": {attribute: rows}}],
    }


def test_elhub_registered_with_explicit_norwegian_coverage():
    assert PROVIDER in cg.list_providers()
    provider = ElhubProvider()
    assert provider.zones() == {"NO1", "NO2", "NO3", "NO4", "NO5"}
    assert provider.capabilities() == {"load", "generation"}

    router = ZoneRouter()
    router.register_coverage(PROVIDER, provider.capability_zones())
    assert router.resolve("NO5", "generation") == PROVIDER
    assert router.resolve("NO1", "prices") is None


def test_parser_sums_consumption_and_converts_interval_kwh_to_mw():
    frame = _parse_response(_payload(hours=2), "load")
    assert_valid_df(frame, expected_cols=["load_mw"])
    assert frame.iloc[0]["load_mw"] == pytest.approx(3.0)
    assert frame.index[0] == pd.Timestamp("2026-08-31T22:00:00Z")
    assert frame.attrs["license"] == "CC BY 4.0"
    assert frame.attrs["data_updated_at"] == "2026-09-15T17:34:12+02:00"


def test_parser_pivots_production_groups_to_canonical_mw_columns():
    frame = _parse_response(_payload("production"), "generation")
    assert_valid_df(frame, expected_cols=["hydro_mw", "wind_mw"])
    assert frame.iloc[0]["hydro_mw"] == pytest.approx(2.0)
    assert frame.iloc[0]["wind_mw"] == pytest.approx(1.0)


def test_parser_rejects_unknown_production_group():
    payload = _payload("production")
    payload["data"][0]["attributes"]["productionPerGroupMbaHour"][0][
        "productionGroup"
    ] = "fusion"
    with pytest.raises(ProviderError, match="unknown production group"):
        _parse_response(payload, "generation")


def test_parser_rejects_changed_response_shape():
    with pytest.raises(ProviderError, match="invalid data collection"):
        _parse_response({"data": {}}, "load")


def test_provider_chunks_queries_and_sends_official_dataset(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake_get_json(url, params):
        calls.append((url, params))
        return _payload()

    monkeypatch.setattr(module, "get_json", fake_get_json)
    frame = ElhubProvider().get_load(ZONE, "2026-09-01", "2026-10-02")

    assert_valid_df(frame, expected_cols=["load_mw"])
    assert len(calls) == 2
    assert calls[0][0].endswith("/price-areas/NO1")
    assert calls[0][1]["dataset"] == "CONSUMPTION_PER_GROUP_MBA_HOUR"
    assert calls[0][1]["startDate"].endswith("+02:00")


@pytest.mark.skipif(not cache._PARQUET_OK, reason="pyarrow not installed")
def test_public_api_cache_and_timezone(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "_manager", CacheManager(tmp_path))
    calls = 0

    def fake_load(self, zone, start, end, **kwargs):
        nonlocal calls
        calls += 1
        return EnergyFrame(
            {"load_mw": [3.0]},
            index=pd.DatetimeIndex(["2025-01-01T00:00:00Z"], name="utc_time"),
        )

    monkeypatch.setattr(ElhubProvider, "get_load", fake_load)
    cg.reset()
    cg.connect(PROVIDER)
    try:
        first = cg.get_load(ZONE, "2025-01-01", "2025-01-01", source=PROVIDER)
        cg.set_timezone("Europe/Oslo")
        second = cg.get_load(ZONE, "2025-01-01", "2025-01-01", source=PROVIDER)
        assert calls == 1
        assert first.attrs["provider"] == PROVIDER
        assert first.attrs["dataset"] == "load"
        assert str(second.index.tz) == "Europe/Oslo"
    finally:
        cg.reset()


def test_public_api_no_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "_manager", CacheManager(tmp_path))
    calls = 0

    def fake_generation(self, zone, start, end, **kwargs):
        nonlocal calls
        calls += 1
        return EnergyFrame(
            {"hydro_mw": [2.0]},
            index=pd.DatetimeIndex(["2025-01-01T00:00:00Z"], name="utc_time"),
        )

    monkeypatch.setattr(ElhubProvider, "get_generation", fake_generation)
    for _ in range(2):
        frame = cg.get_generation(
            ZONE, "2025-01-01", "2025-01-01", source=PROVIDER, use_cache=False
        )
    assert calls == 2
    assert frame.attrs["provider"] == PROVIDER
    assert not list(tmp_path.iterdir())


@pytest.mark.live
def test_elhub_load_and_generation_live_smoke():
    start, end = "2026-09-01T00:00:00+02:00", "2026-09-01T03:00:00+02:00"
    load = cg.get_load(ZONE, start, end, source=PROVIDER, use_cache=False)
    generation = cg.get_generation(ZONE, start, end, source=PROVIDER, use_cache=False)
    assert_valid_df(load, expected_cols=["load_mw"], min_rows=3)
    assert_valid_df(generation, expected_cols=["hydro_mw", "wind_mw"], min_rows=3)
    assert median_interval(load) == pd.Timedelta("1h")
