"""Integration tests — ENTSOG Transparency Platform (EU gas transmission).

Covers: physical gas flows (operator key, country code, point key),
firm technical capacity, zone resolution logic, and electricity-method stubs.
"""

from __future__ import annotations

import pandas as pd
import pytest

import clarigrid as cg
from clarigrid.core.registry import get_provider

from tests.conftest import assert_valid_df

pytestmark = pytest.mark.live

# Use a slightly wider window to ensure ENTSOG returns enough day records.
GAS_START = "2025-01-06"
GAS_END = "2025-01-10"


# ---------------------------------------------------------------------------
# Gas flows — via public API
# ---------------------------------------------------------------------------


def test_entsog_gas_flows_by_operator_key():
    df = cg.get_gas_flows(
        "BE-TSO-0001", GAS_START, GAS_END, source="entsog", use_cache=False
    )
    assert_valid_df(
        df,
        expected_cols=["flow_kwh_d"],                          # numeric
        cols=["direction", "point_key", "operator_key"],       # string metadata
    )


def test_entsog_gas_flows_by_country_code():
    """2-letter country code 'BE' resolves to Fluxys Belgium (BE-TSO-0001)."""
    df = cg.get_gas_flows("BE", GAS_START, GAS_END, source="entsog", use_cache=False)
    assert_valid_df(df, expected_cols=["flow_kwh_d"], cols=["direction"])


def test_entsog_gas_flows_direction_values():
    """direction column must contain only 'entry' / 'exit' strings."""
    df = cg.get_gas_flows(
        "BE-TSO-0001", GAS_START, GAS_END, source="entsog", use_cache=False
    )
    # Pandas 2.x may infer StringDtype instead of object; check string-like either way.
    assert pd.api.types.is_string_dtype(df["direction"]), (
        f"direction should be string dtype, got {df['direction'].dtype}"
    )
    bad = df.loc[~df["direction"].isin(["entry", "exit"]), "direction"].unique()
    assert len(bad) == 0, f"Unexpected direction values: {bad.tolist()}"


# ---------------------------------------------------------------------------
# Firm technical capacity — accessed directly on the provider instance
# (get_capacity is not yet part of the public top-level API)
# ---------------------------------------------------------------------------


def test_entsog_capacity_by_operator_key():
    provider = get_provider("entsog")
    df = provider.get_capacity("BE-TSO-0001", GAS_START, GAS_END)
    assert_valid_df(
        df,
        expected_cols=["capacity_kwh_d"],
        cols=["direction", "point_key", "operator_key"],
    )


# ---------------------------------------------------------------------------
# Electricity method stubs raise NotImplementedError
# ---------------------------------------------------------------------------


def test_entsog_prices_raises():
    provider = get_provider("entsog")
    with pytest.raises(NotImplementedError):
        provider.get_prices("BE", GAS_START, GAS_END)


def test_entsog_load_raises():
    provider = get_provider("entsog")
    with pytest.raises(NotImplementedError):
        provider.get_load("BE", GAS_START, GAS_END)


def test_entsog_generation_raises():
    provider = get_provider("entsog")
    with pytest.raises(NotImplementedError):
        provider.get_generation("BE", GAS_START, GAS_END)
