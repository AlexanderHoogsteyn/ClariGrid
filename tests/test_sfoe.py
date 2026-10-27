"""Tests for the Swiss Federal Office of Energy provider."""

from __future__ import annotations

import pandas as pd
import pytest

import clarigrid as cg
import clarigrid.core.cache as cache
from clarigrid.core.cache import CacheManager
from clarigrid.core.exceptions import ProviderError
from clarigrid.core.normalise import EnergyFrame
from clarigrid.core.router import ZoneRouter
from clarigrid.providers import sfoe as module
from clarigrid.providers.sfoe import SFOEProvider, _parse_rows
from tests.conftest import assert_valid_df, median_interval

PROVIDER = "sfoe"
ZONE = "CH"


def _metadata() -> dict:
    return {
        "id": module._DATASET_ID,
        "tableName": "ogd110_strom_verbrauch_prognose",
        "schema": [
            {"columnName": name, "dataType": kind}
            for name, kind in module._EXPECTED_SCHEMA.items()
        ],
        "updatedAt": "2026-10-19T00:01:15.407Z",
    }


def _rows() -> list[dict]:
    return [
        {
            "datum": "2026-10-19",
            "endverbrauch_prognose_min_gwh": 131,
            "endverbrauch_prognose_mittel_gwh": 155,
            "endverbrauch_prognose_max_gwh": 180,
        },
        {
            "datum": "2026-10-20",
            "endverbrauch_prognose_min_gwh": 134,
            "endverbrauch_prognose_mittel_gwh": 158,
            "endverbrauch_prognose_max_gwh": 183,
        },
    ]


def test_sfoe_registered_with_explicit_swiss_coverage():
    assert PROVIDER in cg.list_providers()
    provider = SFOEProvider()
    assert provider.zones() == {ZONE}
    assert provider.capabilities() == {"load_forecast"}

    router = ZoneRouter()
    router.register_coverage(PROVIDER, provider.capability_zones())
    assert router.resolve(ZONE, "load_forecast") == PROVIDER
    assert router.resolve(ZONE, "load") is None


def test_parser_normalises_daily_gwh_to_average_mw_with_bounds():
    frame = _parse_rows(_rows(), "2026-10-19T00:01:15.407Z")

    assert_valid_df(
        frame,
        expected_cols=[
            "load_forecast_min_mw",
            "load_forecast_mw",
            "load_forecast_max_mw",
        ],
        min_rows=2,
    )
    assert median_interval(frame) == pd.Timedelta("1D")
    assert frame.index[0] == pd.Timestamp("2026-10-18T22:00:00Z")
    assert frame.iloc[0]["load_forecast_mw"] == pytest.approx(155_000 / 24)
    assert frame.attrs["source_unit"] == "GWh/day"
    assert frame.attrs["unit"] == "MW (daily average)"
    assert frame.attrs["data_updated_at"] == "2026-10-19T00:01:15.407Z"
    assert frame.attrs["query_limit"] == 1000
    assert frame.attrs["rate_limit_requests_per_second"] is None
    assert "source attribution required" in frame.attrs["license"]


def test_parser_uses_actual_swiss_dst_day_length():
    rows = [
        {
            "datum": "2025-03-30",
            "endverbrauch_prognose_min_gwh": 23,
            "endverbrauch_prognose_mittel_gwh": 23,
            "endverbrauch_prognose_max_gwh": 23,
        },
        {
            "datum": "2025-10-26",
            "endverbrauch_prognose_min_gwh": 25,
            "endverbrauch_prognose_mittel_gwh": 25,
            "endverbrauch_prognose_max_gwh": 25,
        },
    ]
    frame = _parse_rows(rows, "2026-10-19T00:01:15.407Z")

    assert frame.loc[pd.Timestamp("2025-03-29T23:00:00Z"), "load_forecast_mw"] == 1000
    assert frame.loc[pd.Timestamp("2025-10-25T22:00:00Z"), "load_forecast_mw"] == 1000


def test_parser_rejects_schema_value_and_confidence_changes():
    with pytest.raises(ProviderError, match="missing required fields"):
        _parse_rows([{"datum": "2026-10-19"}], "2026-10-19T00:01:15.407Z")

    rows = _rows()
    rows[0]["endverbrauch_prognose_mittel_gwh"] = "unknown"
    with pytest.raises(ProviderError, match="non-numeric"):
        _parse_rows(rows, "2026-10-19T00:01:15.407Z")

    rows = _rows()
    rows[0]["endverbrauch_prognose_min_gwh"] = 200
    with pytest.raises(ProviderError, match="confidence bounds"):
        _parse_rows(rows, "2026-10-19T00:01:15.407Z")


def test_provider_validates_metadata_and_paginates(monkeypatch):
    calls: list[tuple[str, dict | None]] = []
    full_page = [_rows()[0] for _ in range(module._PAGE_SIZE)]
    for index, row in enumerate(full_page):
        row = dict(row)
        row["datum"] = (pd.Timestamp("2023-01-01") + pd.Timedelta(days=index)).date().isoformat()
        full_page[index] = row

    def fake_get_json(url, params=None):
        calls.append((url, None if params is None else dict(params)))
        if url == module._METADATA_URL:
            return _metadata()
        page = full_page if params["offset"] == 0 else [_rows()[1]]
        return {"data": page, "meta": {"count": len(page), **params}}

    monkeypatch.setattr(module, "get_json", fake_get_json)
    frame = SFOEProvider().get_load_forecast(ZONE, "2023-01-01", "2026-10-20")

    assert len(frame) == 1001
    assert calls[1][1] == {
        "from": "2023-01-01",
        "to": "2026-10-20",
        "limit": 1000,
        "offset": 0,
    }
    assert calls[2][1]["offset"] == 1000

    changed = _metadata()
    changed["schema"][1]["dataType"] = "text"
    monkeypatch.setattr(module, "get_json", lambda *args, **kwargs: changed)
    with pytest.raises(ProviderError, match="changed the load-forecast dataset schema"):
        SFOEProvider().get_load_forecast(ZONE, "2026-10-19", "2026-10-20")


@pytest.mark.skipif(not cache._PARQUET_OK, reason="pyarrow not installed")
def test_public_api_cache_timezone_and_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "_manager", CacheManager(tmp_path))
    calls = 0

    def fake_forecast(self, zone, start, end, **kwargs):
        nonlocal calls
        calls += 1
        frame = EnergyFrame(
            {"load_forecast_mw": [6458.33]},
            index=pd.DatetimeIndex(["2026-10-19T00:00:00Z"], name="utc_time"),
        )
        frame.attrs["license"] = "Open use; source attribution required"
        return frame

    monkeypatch.setattr(SFOEProvider, "get_load_forecast", fake_forecast)
    cg.reset()
    cg.connect(PROVIDER)
    try:
        first = cg.get_load_forecast(ZONE, "2026-10-19", "2026-10-19")
        cg.set_timezone("Europe/Zurich")
        second = cg.get_load_forecast(ZONE, "2026-10-19", "2026-10-19")
        assert calls == 1
        assert first.attrs["provider"] == PROVIDER
        assert first.attrs["dataset"] == "load_forecast"
        assert first.attrs["zone"] == ZONE
        assert first.attrs["license"] == "Open use; source attribution required"
        assert str(second.index.tz) == "Europe/Zurich"
    finally:
        cg.reset()


def test_public_api_no_cache_and_zone_validation(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "_manager", CacheManager(tmp_path))
    calls = 0

    with pytest.raises(ValueError, match="only covers zone 'CH'"):
        SFOEProvider().get_load_forecast("DE_LU", "2026-10-19", "2026-10-19")

    def fake_forecast(self, zone, start, end, **kwargs):
        nonlocal calls
        calls += 1
        return EnergyFrame(
            {"load_forecast_mw": [6458.33]},
            index=pd.DatetimeIndex(["2026-10-19T00:00:00Z"], name="utc_time"),
        )

    monkeypatch.setattr(SFOEProvider, "get_load_forecast", fake_forecast)
    for _ in range(2):
        frame = cg.get_load_forecast(
            ZONE, "2026-10-19", "2026-10-19", source=PROVIDER, use_cache=False
        )
    assert calls == 2
    assert frame.attrs["provider"] == PROVIDER
    assert not list(tmp_path.iterdir())


@pytest.mark.live
def test_sfoe_load_forecast_live_smoke():
    today = pd.Timestamp.now(tz="UTC").normalize()
    frame = cg.get_load_forecast(
        ZONE,
        today,
        today + pd.Timedelta(days=1),
        source=PROVIDER,
        use_cache=False,
    )
    assert_valid_df(
        frame,
        expected_cols=[
            "load_forecast_min_mw",
            "load_forecast_mw",
            "load_forecast_max_mw",
        ],
        min_rows=1,
    )
    assert (frame["load_forecast_min_mw"] <= frame["load_forecast_mw"]).all()
    assert (frame["load_forecast_mw"] <= frame["load_forecast_max_mw"]).all()
