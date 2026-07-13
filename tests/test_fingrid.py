"""Tests for the Fingrid Open Data provider."""

from __future__ import annotations

import pandas as pd
import pytest

import clarigrid as cg
from clarigrid.core import config
from clarigrid.providers import fingrid as module
from clarigrid.providers.fingrid import FingridProvider, _records_to_frame
from tests.conftest import END, START, assert_valid_df

HAS_KEY = bool(config.get_api_key("fingrid"))


def test_fingrid_registered_with_capability_specific_fi_coverage():
    assert "fingrid" in cg.list_providers()
    provider = FingridProvider()
    assert provider.zones() == {"FI"}
    assert {
        "load", "generation", "generation_forecast", "physical_flows",
        "co2_intensity", "imbalance_prices", "balancing_volumes",
    } <= provider.capabilities()


def test_fingrid_long_rows_pivot_to_utc_wide_frame():
    records = [
        {
            "datasetId": 124,
            "startTime": "2025-01-01T00:00:00.000Z",
            "endTime": "2025-01-01T00:15:00.000Z",
            "value": 10,
        },
        {
            "datasetId": 74,
            "startTime": "2025-01-01T00:00:00.000Z",
            "endTime": "2025-01-01T00:15:00.000Z",
            "value": "12.5",
        },
    ]
    frame = _records_to_frame(
        records, {124: "load_mw", 74: "total_generation_mw"}
    )
    assert list(frame.columns) == ["load_mw", "total_generation_mw"]
    assert frame.index.name == "utc_time"
    assert str(frame.index.tz) == "UTC"
    assert frame.iloc[0]["total_generation_mw"] == pytest.approx(12.5)


def test_fingrid_flow_sign_is_converted_to_import_positive(monkeypatch):
    records = [
        {
            "datasetId": 55,
            "startTime": "2025-01-01T00:00:00Z",
            "value": 200.0,  # Fingrid: export from FI
        },
        {
            "datasetId": 57,
            "startTime": "2025-01-01T00:00:00Z",
            "value": -50.0,  # Fingrid: import to FI
        },
    ]
    monkeypatch.setattr(module, "_fetch", lambda *args, **kwargs: records)
    monkeypatch.setattr(module.config, "get_api_key", lambda provider: "test-key")

    frame = FingridProvider().get_physical_flows(
        "FI", "2025-01-01", "2025-01-02"
    )
    assert frame.iloc[0]["flow_ee_mw"] == pytest.approx(-200.0)
    assert frame.iloc[0]["flow_no4_mw"] == pytest.approx(50.0)
    assert frame.attrs["sign_convention"] == "positive=import, negative=export"


def test_fingrid_fetch_paginates_multi_dataset_endpoint(monkeypatch):
    calls: list[dict] = []

    def fake_get(params, api_key):
        calls.append(dict(params))
        page = params["page"]
        return {
            "data": [
                {
                    "datasetId": 124,
                    "startTime": f"2025-01-01T0{page}:00:00Z",
                    "value": page,
                },
                {
                    "datasetId": 74,
                    "startTime": f"2025-01-01T0{page}:00:00Z",
                    "value": page,
                },
            ] if page == 1 else [
                {
                    "datasetId": 124,
                    "startTime": "2025-01-01T02:00:00Z",
                    "value": 2,
                }
            ],
            "pagination": {"currentPage": page, "lastPage": 2},
        }

    monkeypatch.setattr(module, "_PAGE_SIZE", 2)
    monkeypatch.setattr(module, "_throttled_get", fake_get)
    records = module._fetch([124, 74], "2025-01-01", "2025-01-02", "key")

    assert len(records) == 3
    assert [call["page"] for call in calls] == [1, 2]
    assert calls[0]["datasets"] == "124,74"


def test_fingrid_missing_key_has_setup_instructions(monkeypatch):
    monkeypatch.setattr(module.config, "get_api_key", lambda provider: None)
    with pytest.raises(RuntimeError, match="clarigrid keys set fingrid"):
        FingridProvider().get_load("FI", "2025-01-01", "2025-01-02")


@pytest.mark.live
@pytest.mark.skipif(not HAS_KEY, reason="Fingrid API key not configured")
def test_fingrid_load_and_generation_live():
    load = cg.get_load("FI", START, END, source="fingrid", use_cache=False)
    generation = cg.get_generation(
        "FI", START, END, source="fingrid", use_cache=False
    )
    assert_valid_df(load, expected_cols=["load_mw"])
    assert_valid_df(generation, expected_cols=["total_generation_mw", "wind_onshore_mw"])


@pytest.mark.live
@pytest.mark.skipif(not HAS_KEY, reason="Fingrid API key not configured")
def test_fingrid_forecasts_live():
    start = pd.Timestamp.now(tz="UTC").floor("h")
    end = start + pd.Timedelta(hours=12)
    load = cg.get_load_forecast(
        "FI", start, end, source="fingrid", use_cache=False
    )
    generation = cg.get_generation_forecast(
        "FI", start, end, source="fingrid", use_cache=False
    )
    assert_valid_df(load, expected_cols=["load_forecast_mw"])
    assert_valid_df(
        generation,
        expected_cols=["wind_onshore_forecast_mw", "solar_forecast_mw"],
    )


@pytest.mark.live
@pytest.mark.skipif(not HAS_KEY, reason="Fingrid API key not configured")
def test_fingrid_system_series_live():
    flows = cg.get_physical_flows(
        "FI", START, END, source="fingrid", use_cache=False
    )
    carbon = cg.get_co2_intensity(
        "FI", START, END, source="fingrid", use_cache=False
    )
    assert_valid_df(flows, expected_cols=["flow_ee_mw", "flow_se1_mw"])
    assert_valid_df(
        carbon,
        expected_cols=["co2_consumption_g_kwh", "co2_production_g_kwh"],
    )
