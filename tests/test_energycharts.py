"""Tests for the Fraunhofer ISE Energy-Charts provider."""

from __future__ import annotations

import pandas as pd
import pytest

import clarigrid as cg
from clarigrid.core.router import ZoneRouter
from clarigrid.providers.energycharts import (
    EnergyChartsProvider,
    _named_frame,
    _time_frame,
)
from tests.conftest import END, START, assert_valid_df


def test_energycharts_registered():
    assert "energycharts" in cg.list_providers()


def test_energycharts_per_capability_coverage():
    provider = EnergyChartsProvider()
    coverage = provider.capability_zones()

    assert "BE" in coverage["prices"]
    assert "ES" not in coverage["prices"]  # restricted by source licensing
    assert "ES" in coverage["generation"]
    assert "DE_LU" in coverage["frequency"]
    assert "DK2" not in coverage["frequency"]

    router = ZoneRouter()
    router.register_coverage("energycharts", coverage)
    assert router.resolve("BE", "prices") == "energycharts"
    assert router.resolve("ES", "prices") is None
    assert router.resolve("ES", "generation") == "energycharts"


def test_energycharts_time_frame_is_utc_and_numeric():
    frame = _time_frame(
        [1_735_603_200, 1_735_604_100],
        {"load_mw": ["100", None]},
    )
    assert isinstance(frame.index, pd.DatetimeIndex)
    assert str(frame.index.tz) == "UTC"
    assert frame.index.name == "utc_time"
    assert pd.api.types.is_numeric_dtype(frame["load_mw"])
    assert pd.isna(frame["load_mw"].iloc[1])


def test_energycharts_cross_border_gw_to_mw_conversion():
    payload = {
        "unix_seconds": [1_735_603_200],
        "countries": [{"name": "Belgium", "data": [-0.425]}],
    }
    frame = _named_frame(payload, "countries", multiplier=1000.0, prefix="flow_")
    assert frame.loc[frame.index[0], "flow_belgium_mw"] == pytest.approx(-425.0)


@pytest.mark.live
def test_energycharts_prices():
    frame = cg.get_prices("DE", START, END, source="energycharts", use_cache=False)
    assert_valid_df(frame, expected_cols=["price_mwh"])
    assert frame.attrs.get("currency") == "EUR"


@pytest.mark.live
def test_energycharts_load_and_generation():
    load = cg.get_load("DE", START, END, source="energycharts", use_cache=False)
    generation = cg.get_generation("DE", START, END, source="energycharts", use_cache=False)
    assert_valid_df(load, expected_cols=["load_mw"])
    assert_valid_df(generation, expected_cols=["solar_mw", "wind_onshore_mw", "gas_mw"])


@pytest.mark.live
def test_energycharts_forecasts():
    generation = cg.get_generation_forecast(
        "DE", START, END, source="energycharts", use_cache=False
    )
    load = cg.get_load_forecast("DE", START, END, source="energycharts", use_cache=False)
    assert_valid_df(
        generation,
        expected_cols=[
            "solar_forecast_mw",
            "wind_onshore_forecast_mw",
            "wind_offshore_forecast_mw",
        ],
    )
    assert_valid_df(load, expected_cols=["load_forecast_mw"])


@pytest.mark.live
def test_energycharts_cross_border_data_is_mw():
    flows = cg.get_physical_flows("DE", START, END, source="energycharts", use_cache=False)
    schedules = cg.get_commercial_schedule(
        "DE", START, END, source="energycharts", use_cache=False
    )
    assert_valid_df(flows, expected_cols=["flow_belgium_mw"])
    assert_valid_df(schedules, expected_cols=["schedule_belgium_mw"])
    assert flows.attrs.get("sign_convention") == "positive=import, negative=export"


@pytest.mark.live
def test_energycharts_installed_capacity_converted_to_mw():
    frame = cg.get_installed_capacity(
        "DE", "2023", "2025", source="energycharts", use_cache=False
    )
    assert_valid_df(frame, expected_cols=["solar_ac_capacity_mw"])
    assert frame["solar_ac_capacity_mw"].max() > 1_000


@pytest.mark.live
def test_energycharts_frequency_one_second_data():
    frame = cg.get_frequency(
        "DE",
        "2025-01-06T00:00:00Z",
        "2025-01-06T00:01:00Z",
        source="energycharts",
        use_cache=False,
    )
    assert_valid_df(frame, expected_cols=["frequency_hz"], min_rows=50)
    assert frame["frequency_hz"].between(49.0, 51.0).all()


@pytest.mark.live
def test_energycharts_renewable_share():
    frame = cg.get_renewable_share(
        "DE", START, END, source="energycharts", use_cache=False
    )
    assert_valid_df(
        frame,
        expected_cols=["renewable_share_load_pct", "renewable_share_generation_pct"],
    )
    assert frame["renewable_share_load_pct"].dropna().between(0, 100).all()


@pytest.mark.live
def test_energycharts_generation_share():
    frame = cg.get_generation_share(
        "DE", START, END, source="energycharts", use_cache=False
    )
    assert_valid_df(frame, expected_cols=["solar_share_pct", "wind_onshore_share_pct"])
    assert frame.sum(axis=1).dropna().between(99.9, 100.1).all()
