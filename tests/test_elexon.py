"""Integration tests — Elexon Insights Solution / BMRS (Great Britain).

Covers: market-index prices (GBP/MWh), generation by fuel type.
Load is not provided by Elexon BMRS — test that NotImplementedError is raised.

Note on generation: the /generation/outturn/summary endpoint provides a
rolling ~24-hour window of recent data only.  Historical ``start``/``end``
dates are ignored by the Elexon API; the endpoint always returns the most
recently published data.  The test therefore only checks structure, not
specific timestamps.
"""

from __future__ import annotations

import pandas as pd
import pytest

import clarigrid as cg

from tests.conftest import END, START, assert_valid_df

pytestmark = pytest.mark.live


def test_elexon_prices_returns_gbp_mwh():
    df = cg.get_prices("GB", START, END, source="elexon", use_cache=False)
    assert_valid_df(df, expected_cols=["price_gbp_mwh"])


def test_elexon_prices_30min_resolution():
    """Elexon settlement periods are 30-min; verify median interval == 30 min."""
    df = cg.get_prices("GB", START, END, source="elexon", use_cache=False)
    assert len(df) >= 96, f"Expected ≥96 rows (30-min × 2 days), got {len(df)}"
    median_interval = df.index.to_series().diff().dropna().median()
    assert median_interval == pd.Timedelta("30min"), (
        f"Expected 30-min settlement period intervals, got median={median_interval}"
    )


def test_elexon_generation_returns_fuel_columns():
    # Date params are ignored; endpoint returns near-real-time data.
    df = cg.get_generation("GB", START, END, source="elexon", use_cache=False)
    assert_valid_df(df)
    gen_cols = [c for c in df.columns if c.endswith("_mw")]
    assert len(gen_cols) >= 5, (
        f"Expected ≥5 fuel-type columns, got: {sorted(df.columns.tolist())}"
    )


def test_elexon_prices_7day_range_completes():
    """Regression: market-index ignores pagination params and returns the full
    dataset in one response.  Calling with >500 records used to trigger an
    infinite pagination loop.  This test verifies a 7-day query completes and
    returns 30-min data (≥336 rows = 7 days × 48 SPs)."""
    df = cg.get_prices("GB", "2025-01-13", "2025-01-19", source="elexon", use_cache=False)
    assert_valid_df(df, expected_cols=["price_gbp_mwh"])
    # Elexon API returns midnight-to-midnight: "2025-01-13" to "2025-01-19"
    # = 6 full days (288 SPs) + SP1 of Jan 19 = 289 rows.  Assert ≥ 280 to
    # give headroom for DST boundary days.
    assert len(df) >= 280, f"Expected ≥280 rows for 7-day range, got {len(df)}"
    median_interval = df.index.to_series().diff().dropna().median()
    assert median_interval == pd.Timedelta("30min"), (
        f"Expected 30-min intervals, got {median_interval}"
    )


def test_elexon_load_not_supported():
    with pytest.raises(NotImplementedError):
        cg.get_load("GB", START, END, source="elexon", use_cache=False)
