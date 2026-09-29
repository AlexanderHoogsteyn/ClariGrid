"""Tests for the Polskie Sieci Elektroenergetyczne provider."""

from __future__ import annotations

import pandas as pd
import pytest

import clarigrid as cg
import clarigrid.core.cache as cache
from clarigrid.core.cache import CacheManager
from clarigrid.core.exceptions import ProviderError
from clarigrid.core.normalise import EnergyFrame
from clarigrid.core.router import ZoneRouter
from clarigrid.providers import pse as module
from clarigrid.providers.pse import PSEProvider, _parse_rows
from tests.conftest import assert_valid_df, median_interval

PROVIDER = "pse"
ZONE = "PL"


def _rows(field: str = "load_actual") -> list[dict]:
    return [
        {
            "dtime_utc": "2025-01-05 23:15:00",
            "period_utc": "23:00 - 23:15",
            field: 18_500.25,
            "publication_ts_utc": "2025-01-06 01:00:00",
        },
        {
            "dtime_utc": "2025-01-05 23:30:00",
            "period_utc": "23:15 - 23:30",
            field: 18_750.5,
            "publication_ts_utc": "2025-01-06 01:00:00",
        },
    ]


def test_pse_registered_with_explicit_polish_coverage():
    assert PROVIDER in cg.list_providers()
    provider = PSEProvider()
    assert provider.zones() == {ZONE}
    assert provider.capabilities() == {"load", "load_forecast"}

    router = ZoneRouter()
    router.register_coverage(PROVIDER, provider.capability_zones())
    assert router.resolve(ZONE, "load") == PROVIDER
    assert router.resolve(ZONE, "generation") is None


def test_parser_normalises_interval_end_to_start_and_mw_metadata():
    frame = _parse_rows(_rows(), "load_actual")
    forecast = _parse_rows(_rows("load_fcst"), "load_fcst")

    assert_valid_df(frame, expected_cols=["load_mw"], min_rows=2)
    assert_valid_df(forecast, expected_cols=["load_forecast_mw"], min_rows=2)
    assert frame.index[0] == pd.Timestamp("2025-01-05T23:00:00Z")
    assert frame.iloc[0]["load_mw"] == pytest.approx(18_500.25)
    assert frame.attrs["unit"] == "MW"
    assert frame.attrs["data_updated_at"] == "2025-01-06T01:00:00+00:00"
    assert "przetworzona" in frame.attrs["reuse_notice"]


def test_parser_rejects_schema_and_resolution_changes():
    rows = _rows()
    rows[0]["period_utc"] = "23:00 - 23:30"
    with pytest.raises(ProviderError, match="unexpected 30-minute interval"):
        _parse_rows(rows, "load_actual")

    with pytest.raises(ProviderError, match="missing required fields"):
        _parse_rows([{"dtime_utc": "2025-01-01 00:15:00"}], "load_actual")


def test_provider_uses_literal_odata_keys_and_follows_cursor(monkeypatch):
    calls: list[str] = []
    next_link = f"{module._ENDPOINT}?$after=cursor"

    def fake_get_json(url):
        calls.append(url)
        return {"value": _rows()[:1], "nextLink": next_link} if len(calls) == 1 else {
            "value": _rows()[1:]
        }

    monkeypatch.setattr(module, "get_json", fake_get_json)
    frame = PSEProvider().get_load(ZONE, "2025-01-06", "2025-01-06")

    assert_valid_df(frame, expected_cols=["load_mw"], min_rows=2)
    assert len(calls) == 2
    assert "$filter=" in calls[0]
    assert "%24filter" not in calls[0]
    assert "2025-01-05%2023%3A00%3A00" in calls[0]
    assert calls[1] == next_link


@pytest.mark.skipif(not cache._PARQUET_OK, reason="pyarrow not installed")
def test_public_api_cache_and_timezone(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "_manager", CacheManager(tmp_path))
    calls = 0

    def fake_load(self, zone, start, end, **kwargs):
        nonlocal calls
        calls += 1
        return EnergyFrame(
            {"load_mw": [18_500.0]},
            index=pd.DatetimeIndex(["2025-01-05T23:00:00Z"], name="utc_time"),
        )

    monkeypatch.setattr(PSEProvider, "get_load", fake_load)
    cg.reset()
    cg.connect(PROVIDER)
    try:
        first = cg.get_load(ZONE, "2025-01-06", "2025-01-06", source=PROVIDER)
        cg.set_timezone("Europe/Warsaw")
        second = cg.get_load(ZONE, "2025-01-06", "2025-01-06", source=PROVIDER)
        assert calls == 1
        assert first.attrs["provider"] == PROVIDER
        assert first.attrs["dataset"] == "load"
        assert str(second.index.tz) == "Europe/Warsaw"
    finally:
        cg.reset()


def test_public_api_forecast_no_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "_manager", CacheManager(tmp_path))
    calls = 0

    def fake_forecast(self, zone, start, end, **kwargs):
        nonlocal calls
        calls += 1
        return EnergyFrame(
            {"load_forecast_mw": [18_750.0]},
            index=pd.DatetimeIndex(["2025-01-05T23:00:00Z"], name="utc_time"),
        )

    monkeypatch.setattr(PSEProvider, "get_load_forecast", fake_forecast)
    for _ in range(2):
        frame = cg.get_load_forecast(
            ZONE, "2025-01-06", "2025-01-06", source=PROVIDER, use_cache=False
        )
    assert calls == 2
    assert frame.attrs["provider"] == PROVIDER
    assert frame.attrs["dataset"] == "load_forecast"
    assert not list(tmp_path.iterdir())


@pytest.mark.live
def test_pse_load_and_forecast_live_smoke():
    start, end = "2025-01-06T00:00+01:00", "2025-01-06T01:00+01:00"
    load = cg.get_load(ZONE, start, end, source=PROVIDER, use_cache=False)
    forecast = cg.get_load_forecast(ZONE, start, end, source=PROVIDER, use_cache=False)
    assert_valid_df(load, expected_cols=["load_mw"], min_rows=4)
    assert_valid_df(forecast, expected_cols=["load_forecast_mw"], min_rows=4)
    assert median_interval(load) == pd.Timedelta("15min")
