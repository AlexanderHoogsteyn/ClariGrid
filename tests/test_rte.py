"""Tests for the RTE Eco2mix provider."""

from __future__ import annotations

import pandas as pd
import pytest

import clarigrid as cg
from clarigrid.core.router import ZoneRouter
from clarigrid.providers import rte as module
from clarigrid.providers.rte import RTEProvider, _generation_frame, _records_frame
from tests.conftest import assert_valid_df


def _records():
    return [
        {
            "date_heure": "2025-01-06T00:00:00Z",
            "consommation": 53000,
            "prevision_j": 52900,
            "prevision_j1": 52800,
            "eolien": 10000,
            "eolien_terrestre": 9000,
            "eolien_offshore": 1000,
            "solaire": 500,
            "nucleaire": 38000,
            "taux_co2": 18,
            "ech_physiques": -2500,
        },
        {
            "date_heure": "2025-01-06T00:15:00Z",
            "consommation": None,
            "prevision_j": 52700,
            "prevision_j1": 52600,
            "eolien": None,
            "eolien_terrestre": None,
            "eolien_offshore": None,
            "solaire": None,
            "nucleaire": None,
            "taux_co2": None,
            "ech_physiques": None,
        },
    ]


def test_rte_registered_with_capability_specific_fr_coverage():
    assert "rte" in cg.list_providers()
    provider = RTEProvider()
    assert "prices" not in provider.capabilities()
    router = ZoneRouter()
    router.register_coverage("rte", provider.capability_zones())
    assert router.resolve("FR", "co2_intensity") == "rte"
    assert router.resolve("FR", "prices") is None


def test_rte_historical_null_quarter_hours_are_removed_for_actuals():
    load = _records_frame(_records(), {"consommation": "load_mw"})
    forecast = _records_frame(_records(), {"prevision_j": "load_forecast_mw"})
    assert len(load) == 1
    assert len(forecast) == 2
    assert str(load.index.tz) == "UTC"


def test_rte_generation_uses_detailed_wind_without_double_counting():
    frame = _generation_frame(_records())
    assert frame.iloc[0]["wind_onshore_mw"] == 9000
    assert frame.iloc[0]["wind_offshore_mw"] == 1000
    assert "wind_total_mw" not in frame


def test_rte_legacy_aggregate_wind_falls_back_to_onshore():
    records = [{
        "date_heure": "2013-01-01T00:00:00Z",
        "eolien": 7500,
        "eolien_terrestre": None,
        "eolien_offshore": None,
    }]
    frame = _generation_frame(records)
    assert frame.iloc[0]["wind_onshore_mw"] == 7500


def test_rte_provider_normalises_load_forecasts_flow_and_co2(monkeypatch):
    monkeypatch.setattr(RTEProvider, "_records", lambda *args, **kwargs: _records())
    provider = RTEProvider()
    load = provider.get_load("FR", "2025-01-06", "2025-01-07")
    forecast = provider.get_load_forecast("FR", "2025-01-06", "2025-01-07")
    flow = provider.get_physical_flows("FR", "2025-01-06", "2025-01-07")
    co2 = provider.get_co2_intensity("FR", "2025-01-06", "2025-01-07")
    assert list(load) == ["load_mw"]
    assert set(forecast) == {"load_forecast_mw", "load_day_ahead_forecast_mw"}
    assert flow.iloc[0]["flow_net_mw"] == -2500
    assert flow.attrs["sign_convention"] == "positive=import, negative=export"
    assert co2.iloc[0]["co2_production_g_kwh"] == 18


def test_rte_fetch_prefers_realtime_on_timestamp_overlap(monkeypatch):
    module._RAW_CACHE.clear()
    historical = [{"date_heure": "2025-01-06T00:00:00Z", "consommation": 100}]
    realtime = [{"date_heure": "2025-01-06T00:00:00Z", "consommation": 200}]

    def fake_fetch(url, start, end):
        return realtime if url == module._REALTIME_URL else historical

    monkeypatch.setattr(module, "_fetch_dataset", fake_fetch)
    records = module._fetch_records("2025-01-06", "2025-01-07")
    assert records[0]["consommation"] == 200


@pytest.mark.live
def test_rte_load_generation_forecast_and_co2_live():
    start, end = "2025-01-06T00:00:00Z", "2025-01-06T02:00:00Z"
    load = cg.get_load("FR", start, end, source="rte", use_cache=False)
    generation = cg.get_generation("FR", start, end, source="rte", use_cache=False)
    forecast = cg.get_load_forecast("FR", start, end, source="rte", use_cache=False)
    co2 = cg.get_co2_intensity("FR", start, end, source="rte", use_cache=False)
    assert_valid_df(load, expected_cols=["load_mw"], min_rows=4)
    assert_valid_df(
        generation, expected_cols=["nuclear_mw", "wind_onshore_mw", "solar_mw"]
    )
    assert_valid_df(forecast, expected_cols=["load_forecast_mw"], min_rows=8)
    assert_valid_df(co2, expected_cols=["co2_production_g_kwh"])


@pytest.mark.live
def test_rte_flows_and_generation_shares_live():
    start, end = "2025-01-06T00:00:00Z", "2025-01-06T02:00:00Z"
    flow = cg.get_physical_flows("FR", start, end, source="rte", use_cache=False)
    shares = cg.get_generation_share("FR", start, end, source="rte", use_cache=False)
    assert_valid_df(flow, expected_cols=["flow_net_mw"])
    assert_valid_df(shares, expected_cols=["nuclear_share_pct"])
    assert shares.sum(axis=1).dropna().between(99.9, 100.1).all()
