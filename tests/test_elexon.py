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

import pytest

import clarigrid as cg

from tests.conftest import END, START, assert_valid_df

pytestmark = pytest.mark.live


def test_elexon_prices_returns_gbp_mwh():
    df = cg.get_prices("GB", START, END, source="elexon", use_cache=False)
    assert_valid_df(df, expected_cols=["price_gbp_mwh"])


def test_elexon_prices_30min_resolution():
    """Elexon settlement periods are 30-min; 2 days → ≥96 rows."""
    df = cg.get_prices("GB", START, END, source="elexon", use_cache=False)
    assert len(df) >= 96, f"Expected ≥96 rows (30-min × 2 days), got {len(df)}"


def test_elexon_generation_returns_fuel_columns():
    # Date params are ignored; endpoint returns near-real-time data.
    df = cg.get_generation("GB", START, END, source="elexon", use_cache=False)
    assert_valid_df(df)
    gen_cols = [c for c in df.columns if c.endswith("_mw")]
    assert len(gen_cols) >= 5, (
        f"Expected ≥5 fuel-type columns, got: {sorted(df.columns.tolist())}"
    )


def test_elexon_load_not_supported():
    with pytest.raises(NotImplementedError):
        cg.get_load("GB", START, END, source="elexon", use_cache=False)
