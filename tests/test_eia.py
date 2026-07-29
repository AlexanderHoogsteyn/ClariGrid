"""Tests for the U.S. EIA-930 provider."""

from __future__ import annotations

import pandas as pd
import pytest

import clarigrid as cg
import clarigrid.providers.eia as module
from clarigrid.core.router import ZoneRouter
from clarigrid.providers.eia import EIAProvider, _interchange_frame, _pivot
from tests.conftest import END, START, assert_valid_df

HAS_KEY = bool(module.config.get_api_key("eia"))


def test_eia_registered_and_alias_coverage():
    assert "eia" in cg.list_providers()
    coverage = EIAProvider().capability_zones()
    assert "CISO" in coverage["load"]
    assert "prices" not in coverage

    router = ZoneRouter()
    router.register_coverage("eia", coverage)
    assert router.resolve("CISO", "generation") == "eia"
    assert router.resolve("ERCO", "load") == "eia"
    assert router.resolve("BE", "load") is None


def test_eia_region_rows_are_pivoted_to_canonical_mw():
    records = [
        {"period": "2025-01-01T00", "type": "D", "value": "25000"},
        {"period": "2025-01-01T00", "type": "DF", "value": "25500"},
        {"period": "2025-01-01T01", "type": "D", "value": "25200"},
    ]
    frame = _pivot(records, "type", module._REGION_COLUMNS)

    assert str(frame.index.tz) == "UTC"
    assert frame.index.name == "utc_time"
    assert frame.loc[pd.Timestamp("2025-01-01T00:00Z"), "load_mw"] == 25000
    assert frame.loc[pd.Timestamp("2025-01-01T00:00Z"), "load_forecast_mw"] == 25500


def test_eia_generation_fuel_codes_use_shared_columns():
    records = [
        {"period": "2025-01-01T00", "fueltype": "NG", "value": "100.5"},
        {"period": "2025-01-01T00", "fueltype": "SUN", "value": "20"},
        {"period": "2025-01-01T00", "fueltype": "WND", "value": "30"},
    ]
    frame = _pivot(records, "fueltype", module._FUEL_COLUMNS)

    assert frame.iloc[0]["gas_mw"] == pytest.approx(100.5)
    assert frame.iloc[0]["solar_mw"] == pytest.approx(20)
    assert frame.iloc[0]["wind_mw"] == pytest.approx(30)


def test_eia_interchange_is_converted_to_import_positive():
    records = [
        {"period": "2025-01-01T00", "toba": "BANC", "value": "125"},
        {"period": "2025-01-01T00", "toba": "LDWP", "value": "-80"},
    ]
    frame = _interchange_frame(records)

    assert frame.iloc[0]["flow_banc_mw"] == pytest.approx(-125)
    assert frame.iloc[0]["flow_ldwp_mw"] == pytest.approx(80)


def test_eia_fetch_paginates(monkeypatch):
    calls: list[dict] = []

    def fake_get(url, params):
        calls.append(dict(params))
        offset = int(params["offset"])
        page = [
            {"period": "2025-01-01T00", "type": "D", "value": "1"},
            {"period": "2025-01-01T01", "type": "D", "value": "2"},
        ] if offset == 0 else [
            {"period": "2025-01-01T02", "type": "D", "value": "3"}
        ]
        return {"response": {"total": 3, "data": page}}

    monkeypatch.setattr(module, "_PAGE_SIZE", 2)
    monkeypatch.setattr(module, "get_json", fake_get)
    monkeypatch.setattr(module, "_api_key", lambda: "test-key")
    records = module._fetch(
        "region-data", START, END, {"respondent": "CISO", "type": "D"}
    )

    assert len(records) == 3
    assert [call["offset"] for call in calls] == [0, 2]
    assert calls[0]["facets[respondent][]"] == "CISO"
    assert calls[0]["facets[type][]"] == "D"


def test_eia_missing_key_has_setup_instructions(monkeypatch):
    monkeypatch.setattr(module.config, "get_api_key", lambda provider: None)
    with pytest.raises(RuntimeError, match="clarigrid keys set eia"):
        EIAProvider().get_load("CISO", START, END)


@pytest.mark.live
@pytest.mark.skipif(not HAS_KEY, reason="EIA API key not configured")
def test_eia_load_generation_and_forecast_live():
    load = cg.get_load("CAISO", START, END, source="eia", use_cache=False)
    forecast = cg.get_load_forecast(
        "CAISO", START, END, source="eia", use_cache=False
    )
    generation = cg.get_generation(
        "CAISO", START, END, source="eia", use_cache=False
    )
    assert_valid_df(load, expected_cols=["load_mw"])
    assert_valid_df(forecast, expected_cols=["load_forecast_mw"])
    assert_valid_df(generation, expected_cols=["gas_mw", "solar_mw", "wind_mw"])


@pytest.mark.live
@pytest.mark.skipif(not HAS_KEY, reason="EIA API key not configured")
def test_eia_interchange_live():
    frame = cg.get_physical_flows(
        "CISO", START, END, source="eia", use_cache=False
    )
    assert_valid_df(frame)
    assert all(column.startswith("flow_") for column in frame.columns)
    assert frame.attrs["sign_convention"] == "positive=import, negative=export"
