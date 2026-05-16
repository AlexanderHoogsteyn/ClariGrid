"""Integration tests — NESO Data Portal (Great Britain electricity).

Covers: load, embedded generation.
Prices are not provided by NESO — test that NotImplementedError is raised.
For full BM generation mix use the Elexon provider (tested separately).
"""

from __future__ import annotations

import pandas as pd
import pytest

import clarigrid as cg

from tests.conftest import END, START, assert_valid_df

pytestmark = pytest.mark.live


def test_neso_load_returns_load_mw():
    df = cg.get_load("GB", START, END, source="neso", use_cache=False)
    assert_valid_df(df, expected_cols=["load_mw"])


def test_neso_load_30min_resolution():
    """GB settlement periods are 30-min; verify row count and median interval == 30 min."""
    df = cg.get_load("GB", START, END, source="neso", use_cache=False)
    assert len(df) >= 96, f"Expected ≥96 rows (30-min × 2 days), got {len(df)}"
    median_interval = df.index.to_series().diff().dropna().median()
    assert median_interval == pd.Timedelta("30min"), (
        f"Expected 30-min settlement period intervals, got median={median_interval}"
    )


def test_neso_generation_embedded_wind_and_solar():
    df = cg.get_generation("GB", START, END, source="neso", use_cache=False)
    assert_valid_df(df, expected_cols=["wind_embedded_mw", "solar_embedded_mw"])


def test_neso_prices_not_supported():
    with pytest.raises(NotImplementedError):
        cg.get_prices("GB", START, END, source="neso", use_cache=False)
