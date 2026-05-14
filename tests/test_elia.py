"""Integration tests — Elia Open Data (Belgium electricity).

Covers: load, generation.
Prices are not published by Elia — test that NotImplementedError is raised.
"""

from __future__ import annotations

import pytest

import clarigrid as cg

from tests.conftest import END, START, assert_valid_df

pytestmark = pytest.mark.live


def test_elia_load_returns_load_mw():
    df = cg.get_load("BE", START, END, source="elia", use_cache=False)
    assert_valid_df(df, expected_cols=["load_mw"])


def test_elia_load_15min_resolution():
    """Elia data is 15-min; 2 days → ≥192 rows."""
    df = cg.get_load("BE", START, END, source="elia", use_cache=False)
    assert len(df) >= 192, f"Expected ≥192 rows (15-min × 2 days), got {len(df)}"


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
