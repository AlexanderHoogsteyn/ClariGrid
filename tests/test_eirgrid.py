"""Tests for the EirGrid Smart Grid Dashboard provider."""

from __future__ import annotations

import pandas as pd
import pytest

import clarigrid as cg
import clarigrid.core.cache as cache
from clarigrid.core.cache import CacheManager
from clarigrid.core.exceptions import ProviderError
from clarigrid.core.normalise import EnergyFrame
from clarigrid.core.router import ZoneRouter
from clarigrid.providers import eirgrid as module
from clarigrid.providers.eirgrid import EirGridProvider, _parse_response
from tests.conftest import assert_valid_df, median_interval

PROVIDER = "eirgrid"


def _payload(region: str = "ROI") -> dict:
    return {
        "Rows": [
            {
                "EffectiveTime": "25-Oct-2026 00:00:00",
                "FieldName": "SYSTEM_DEMAND",
                "Region": region,
                "Value": 3_700,
            },
            {
                "EffectiveTime": "25-Oct-2026 00:15:00",
                "FieldName": "SYSTEM_DEMAND",
                "Region": region,
                "Value": 3_725.5,
            },
            {
                "EffectiveTime": "25-Oct-2026 00:30:00",
                "FieldName": "SYSTEM_DEMAND",
                "Region": region,
                "Value": None,
            },
        ]
    }


def _bounds() -> tuple[pd.Timestamp, pd.Timestamp]:
    return pd.Timestamp("2026-10-25T00:00:00Z"), pd.Timestamp("2026-10-26T00:00:00Z")


def test_eirgrid_registered_with_explicit_load_coverage():
    assert PROVIDER in cg.list_providers()
    provider = EirGridProvider()
    assert provider.zones() == {"IE", "GB_NIR"}
    assert provider.capabilities() == {"load"}

    router = ZoneRouter()
    router.register_coverage(PROVIDER, provider.capability_zones())
    assert router.resolve("IE", "load") == PROVIDER
    assert router.resolve("GB_NIR", "load") == PROVIDER
    assert router.resolve("IE", "generation") is None


def test_parser_normalises_utc_demand_and_preserves_unknown_metadata_as_null():
    start, end = _bounds()
    frame = _parse_response(_payload(), "ROI", start, end)

    assert_valid_df(frame, expected_cols=["load_mw"], min_rows=2)
    assert frame.index[0] == pd.Timestamp("2026-10-25T00:00:00Z")
    assert frame.iloc[1]["load_mw"] == pytest.approx(3_725.5)
    assert frame.attrs["unit"] == "MW"
    assert frame.attrs["api_version"] is None
    assert frame.attrs["api_stability"] is None
    assert frame.attrs["temporal_coverage"] is None
    assert frame.attrs["rate_limit"] is None
    assert frame.attrs["reuse_notice"] == "Supported by EirGrid Group Data"


def test_parser_rejects_schema_region_and_resolution_changes():
    start, end = _bounds()
    with pytest.raises(ProviderError, match="missing required fields"):
        _parse_response({"Rows": [{"EffectiveTime": "25-Oct-2026 00:00:00"}]}, "ROI", start, end)

    wrong_region = _payload("NI")
    with pytest.raises(ProviderError, match="unexpected field or region"):
        _parse_response(wrong_region, "ROI", start, end)

    wrong_interval = _payload()
    wrong_interval["Rows"][1]["EffectiveTime"] = "25-Oct-2026 00:14:00"
    with pytest.raises(ProviderError, match="unexpected demand interval"):
        _parse_response(wrong_interval, "ROI", start, end)


def test_provider_maps_zones_and_sends_bounded_dashboard_query(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake_get_json(url, params):
        calls.append((url, params))
        return _payload("NI")

    monkeypatch.setattr(module, "get_json", fake_get_json)
    frame = EirGridProvider().get_load("GB_NIR", "2026-10-25T00:10:00Z", "2026-10-25T00:20:00Z")

    assert_valid_df(frame, expected_cols=["load_mw"], min_rows=1)
    assert list(frame.index) == [pd.Timestamp("2026-10-25T00:15:00Z")]
    assert calls == [
        (
            module._ENDPOINT,
            {
                "region": "NI",
                "chartType": "demand",
                "dateRange": "day",
                "dateFrom": "2026-10-25",
                "dateTo": "2026-10-25",
                "areas": "demandactual",
            },
        )
    ]


@pytest.mark.skipif(not cache._PARQUET_OK, reason="pyarrow not installed")
def test_public_api_cache_and_timezone(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "_manager", CacheManager(tmp_path))
    calls = 0

    def fake_load(self, zone, start, end, **kwargs):
        nonlocal calls
        calls += 1
        return EnergyFrame(
            {"load_mw": [3_700.0]},
            index=pd.DatetimeIndex(["2026-10-25T00:00:00Z"], name="utc_time"),
        )

    monkeypatch.setattr(EirGridProvider, "get_load", fake_load)
    cg.reset()
    cg.connect(PROVIDER)
    try:
        first = cg.get_load("IE", "2026-10-25", "2026-10-25", source=PROVIDER)
        cg.set_timezone("Europe/Dublin")
        second = cg.get_load("IE", "2026-10-25", "2026-10-25", source=PROVIDER)
        assert calls == 1
        assert first.attrs["provider"] == PROVIDER
        assert first.attrs["dataset"] == "load"
        assert str(second.index.tz) == "Europe/Dublin"
    finally:
        cg.reset()


def test_public_api_no_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "_manager", CacheManager(tmp_path))
    calls = 0

    def fake_load(self, zone, start, end, **kwargs):
        nonlocal calls
        calls += 1
        return EnergyFrame(
            {"load_mw": [3_700.0]},
            index=pd.DatetimeIndex(["2026-10-25T00:00:00Z"], name="utc_time"),
        )

    monkeypatch.setattr(EirGridProvider, "get_load", fake_load)
    for _ in range(2):
        frame = cg.get_load("IE", "2026-10-25", "2026-10-25", source=PROVIDER, use_cache=False)
    assert calls == 2
    assert frame.attrs["provider"] == PROVIDER
    assert not list(tmp_path.iterdir())


@pytest.mark.live
@pytest.mark.parametrize("zone", ["IE", "GB-NIR"])
def test_eirgrid_load_live_smoke(zone):
    frame = cg.get_load(zone, "2026-10-25", "2026-10-25", source=PROVIDER, use_cache=False)
    assert_valid_df(frame, expected_cols=["load_mw"], min_rows=90)
    assert median_interval(frame) == pd.Timedelta("15min")
