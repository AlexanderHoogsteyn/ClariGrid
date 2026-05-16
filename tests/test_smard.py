"""Integration tests — SMARD (Germany electricity).

Covers: prices, load, generation.
All tests call the live SMARD API; mark with ``live``.
"""

from __future__ import annotations

import pytest

import clarigrid as cg

from tests.conftest import END, START, assert_valid_df

pytestmark = pytest.mark.live


def test_smard_prices_returns_price_mwh():
    df = cg.get_prices("DE", START, END, source="smard", use_cache=False)
    assert_valid_df(df, expected_cols=["price_mwh"])
    assert df.attrs.get("currency") == "EUR", (
        f"Expected currency='EUR' in df.attrs, got: {df.attrs}"
    )


def test_smard_load_returns_load_mw():
    df = cg.get_load("DE", START, END, source="smard", use_cache=False)
    assert_valid_df(df, expected_cols=["load_mw"])


def test_smard_generation_returns_fuel_columns():
    df = cg.get_generation("DE", START, END, source="smard", use_cache=False)
    assert_valid_df(df)
    gen_cols = [c for c in df.columns if c.endswith("_mw")]
    assert len(gen_cols) >= 2, (
        f"Expected ≥2 generation columns, got: {sorted(df.columns.tolist())}"
    )


def test_smard_generation_known_fuel_types():
    """Solar and onshore wind must be present; nuclear absent post-April 2023."""
    df = cg.get_generation("DE", START, END, source="smard", use_cache=False)
    present = set(df.columns)
    # Germany still has solar and onshore wind in Jan 2025.
    required = {"solar_mw", "wind_onshore_mw"}
    missing = required - present
    assert not missing, f"Missing fuel columns: {missing}. Got: {sorted(present)}"
    # Nuclear phased out Apr 2023 — must not appear.
    assert "nuclear_mw" not in present, "nuclear_mw unexpectedly present post-2023"
