"""Integration tests — Elia Open Data (Belgium electricity).

Covers: load, generation.
Prices are not published by Elia — test that NotImplementedError is raised.
"""

from __future__ import annotations

import pandas as pd
import pytest

import clarigrid as cg

from tests.conftest import END, START, assert_valid_df

pytestmark = pytest.mark.live


def test_elia_load_returns_load_mw():
    df = cg.get_load("BE", START, END, source="elia", use_cache=False)
    assert_valid_df(df, expected_cols=["load_mw"])


def test_elia_load_15min_resolution():
    """Elia data is 15-min; verify row count and median interval == 15 min."""
    df = cg.get_load("BE", START, END, source="elia", use_cache=False)
    assert len(df) >= 192, f"Expected ≥192 rows (15-min × 2 days), got {len(df)}"
    median_interval = df.index.to_series().diff().dropna().median()
    assert median_interval == pd.Timedelta("15min"), (
        f"Expected 15-min intervals, got median={median_interval}"
    )


def test_elia_generation_returns_fuel_columns():
    df = cg.get_generation("BE", START, END, source="elia", use_cache=False)
    assert_valid_df(df)
    gen_cols = [c for c in df.columns if c.endswith("_mw")]
    assert len(gen_cols) >= 1, (
        f"Expected ≥1 generation column. Got: {sorted(df.columns.tolist())}"
    )


def test_elia_generation_has_solar_and_wind():
    df = cg.get_generation("BE", START, END, source="elia", use_cache=False)
    assert "solar_mw" in df.columns, f"Missing solar_mw. Got: {sorted(df.columns.tolist())}"
    wind_cols = [c for c in df.columns if "wind" in c]
    assert wind_cols, f"No wind column found. Got: {sorted(df.columns.tolist())}"


def test_elia_prices_not_supported():
    with pytest.raises(NotImplementedError):
        cg.get_prices("BE", START, END, source="elia", use_cache=False)


# ── Forecasts ───────────────────────────────────────────────────────────────
def test_elia_load_forecast_has_dayahead():
    df = cg.get_load_forecast("BE", START, END, source="elia", use_cache=False)
    assert_valid_df(df, expected_cols=["dayaheadforecast"])


def test_elia_generation_forecast_has_wind_and_solar():
    df = cg.get_generation_forecast("BE", START, END, source="elia", use_cache=False)
    assert_valid_df(df)
    cols = df.columns.tolist()
    assert any(c.startswith("wind_") for c in cols), f"No wind_ column. Got: {sorted(cols)}"
    assert any(c.startswith("solar_") for c in cols), f"No solar_ column. Got: {sorted(cols)}"


# ── Imbalance & balancing ─────────────────────────────────────────────────────
def test_elia_imbalance_prices_returns_price():
    df = cg.get_imbalance_prices("BE", START, END, source="elia", use_cache=False)
    assert_valid_df(df, expected_cols=["imbalanceprice"])


def test_elia_system_imbalance_returns_si_column():
    df = cg.get_system_imbalance("BE", START, END, source="elia", use_cache=False)
    assert_valid_df(df, expected_cols=["system_imbalance_mw"])


def test_elia_balancing_volumes_returns_numeric_columns():
    df = cg.get_balancing_volumes("BE", START, END, source="elia", use_cache=False)
    assert_valid_df(df)
    assert len(df.columns) >= 1, f"Expected ≥1 volume column. Got: {sorted(df.columns)}"


def test_elia_balancing_prices_returns_numeric_columns():
    df = cg.get_balancing_prices("BE", START, END, source="elia", use_cache=False)
    assert_valid_df(df)
    assert len(df.columns) >= 1, f"Expected ≥1 price column. Got: {sorted(df.columns)}"


# ── Cross-border & capacity ───────────────────────────────────────────────────
def test_elia_physical_flows_per_border():
    df = cg.get_physical_flows("BE", START, END, source="elia", use_cache=False)
    assert_valid_df(df)
    flow_cols = [c for c in df.columns if c.startswith("flow_")]
    assert flow_cols, f"No flow_ columns. Got: {sorted(df.columns)}"


def test_elia_commercial_schedule_per_border():
    df = cg.get_commercial_schedule("BE", START, END, source="elia", use_cache=False)
    assert_valid_df(df)
    assert any(c.startswith("schedule_") for c in df.columns), (
        f"No schedule_ columns. Got: {sorted(df.columns)}"
    )


def test_elia_ntc_per_border():
    df = cg.get_ntc("BE", START, END, source="elia", use_cache=False)
    assert_valid_df(df)
    assert any(c.startswith("ntc_") for c in df.columns), (
        f"No ntc_ columns. Got: {sorted(df.columns)}"
    )


def test_elia_net_position_returns_net_position_mw():
    df = cg.get_net_position("BE", START, END, source="elia", use_cache=False)
    assert_valid_df(df, expected_cols=["net_position_mw"])


# ── Environmental ─────────────────────────────────────────────────────────────
def test_elia_co2_intensity_returns_intensity_columns():
    df = cg.get_co2_intensity("BE", START, END, source="elia", use_cache=False)
    assert_valid_df(
        df, expected_cols=["co2_production_g_kwh", "co2_consumption_g_kwh"]
    )
