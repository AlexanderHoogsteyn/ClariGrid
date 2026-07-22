"""Canonical column names and cross-provider normalisation pipeline.

All DataFrames leaving the public API surface are passed through one of
these functions so that:

- Price data has a single column ``price_mwh`` (currency stored in
  ``df.attrs["currency"]``).
- Load data has column ``load_mw``.
- Generation data has columns like ``wind_mw``, ``solar_mw`` etc., all
  with the ``_mw`` suffix (SMARD uses ``_mwh`` — this is corrected here).

Timezone conversion is handled separately in ``session.apply_output_tz()``.
"""

from __future__ import annotations

import pandas as pd

# ── Canonical output column names ──────────────────────────────────────────

# Prices — per MWh.  Currency stored in df.attrs["currency"].
COL_PRICE = "price_mwh"

# Load
COL_LOAD = "load_mw"

# Gas flows / capacity
COL_GAS_FLOW = "flow_kwh_d"
COL_GAS_CAP  = "capacity_kwh_d"

# Generation — *_mw suffix convention.  Column names are fuel-type specific
# (e.g. ``wind_onshore_mw``, ``solar_mw``, ``nuclear_mw``).  No single
# constant needed — the suffix check in ``normalise_generation`` handles all.

# ── Price column → currency map ────────────────────────────────────────────
# Maps provider-specific column names to their ISO currency code.
_PRICE_COL_MAP: dict[str, str] = {
    "price_usd_mwh": "USD",
    "price_gbp_mwh": "GBP",
    "price_eur_mwh": "EUR",
    "price_mwh":     "EUR",   # already canonical; assume EUR if unset
}


# ── Normalisation functions ────────────────────────────────────────────────

def normalise_prices(df: pd.DataFrame) -> pd.DataFrame:
    """Rename any recognised price column to ``price_mwh``.

    Sets ``df.attrs["currency"]`` to the ISO currency code. Leaves the
    DataFrame untouched if no known price column
    is found.
    """
    if df.empty:
        return df
    for src_col, currency in _PRICE_COL_MAP.items():
        if src_col in df.columns:
            if src_col != COL_PRICE:
                df = df.rename(columns={src_col: COL_PRICE})
            df.attrs.setdefault("currency", currency)
            return df
    return df


def normalise_load(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure the load column is named ``load_mw``.

    No-op if the column is already present.  Renames the first column
    whose name contains "load" or "demand" (case-insensitive), or the
    first numeric column if nothing more specific is found.
    """
    if df.empty or COL_LOAD in df.columns:
        return df
    candidates = [
        c for c in df.columns
        if "load" in c.lower() or "demand" in c.lower()
    ]
    if not candidates:
        candidates = [
            c for c in df.columns
            if pd.api.types.is_numeric_dtype(df[c])
        ]
    if candidates:
        df = df.rename(columns={candidates[0]: COL_LOAD})
    return df


def normalise_generation(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise generation column names.

    - Strips any ``columns.name`` metadata (artefact of ``pivot_table``).
    - Renames columns with ``_mwh`` suffix to ``_mw`` — SMARD exports
      energy-per-hour values (MWh) which are numerically identical to
      average MW for hourly data, so the unit suffix is corrected to
      match the rest of the SDK.
    """
    if df.empty:
        return df
    df = df.copy()
    df.columns.name = None
    rename = {
        col: col[:-4] + "_mw"
        for col in df.columns
        if isinstance(col, str) and col.endswith("_mwh")
    }
    if rename:
        df = df.rename(columns=rename)
    return df
