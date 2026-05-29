"""Canonical column names, cross-provider normalisation pipeline, and EnergyFrame.

All DataFrames leaving the public API surface are:
1. Wrapped in ``EnergyFrame`` — a thin ``pd.DataFrame`` subclass that preserves
   Clarigrid metadata (currency, provider, zone…) through pandas operations such
   as ``resample``, ``concat``, ``copy``, and ``merge``.
2. Passed through one of the ``normalise_*`` functions so that column names and
   units are consistent regardless of the source provider.

Column conventions
------------------
- Prices:     ``price_mwh``       (currency in ``EnergyFrame.currency``)
- Load:       ``load_mw``
- Generation: ``{fuel}_mw``       (always average power in MW, never MWh)
- Gas flows:  ``flow_kwh_d``
- Gas cap:    ``capacity_kwh_d``

Timezone conversion is handled separately in ``session.apply_output_tz()``.
"""

from __future__ import annotations

import pandas as pd

from clarigrid.core.types import (
    COLUMN_GAS_CAP,   # noqa: F401 — re-exported for convenience
    COLUMN_GAS_FLOW,  # noqa: F401
    COLUMN_LOAD,
    COLUMN_PRICE,
)


# ── EnergyFrame ────────────────────────────────────────────────────────────

class EnergyFrame(pd.DataFrame):
    """``pd.DataFrame`` subclass that preserves Clarigrid metadata.

    Metadata survives pandas operations (``resample``, ``concat``, ``copy``,
    ``merge``, slicing) via the ``_metadata`` / ``__finalize__`` mechanism.

    Access metadata via properties::

        df = cg.get_prices("BE", "2025-01-01", "2025-01-07")
        df.currency       # "EUR"
        df.provider       # "elia"
        df.zone           # "BE"
        df.dataset        # "prices"
        df.fetched_at     # "2025-05-28T10:00:00+00:00"

    ``df.attrs`` is also populated for backward compatibility.
    """

    # pandas propagates attributes listed here through finalize().
    _metadata = ["_clarigrid_meta"]

    def __init__(self, data=None, *args, **kwargs):
        super().__init__(data, *args, **kwargs)
        if not hasattr(self, "_clarigrid_meta"):
            object.__setattr__(self, "_clarigrid_meta", {})

    @property
    def _constructor(self):
        return EnergyFrame

    @property
    def _constructor_sliced(self):
        # Keep column slices as plain Series — no metadata on a Series.
        return pd.Series

    def __finalize__(self, other, method=None, **kwargs) -> "EnergyFrame":
        """Copy _clarigrid_meta through all pandas operations."""
        super().__finalize__(other, method=method, **kwargs)
        if isinstance(other, EnergyFrame):
            object.__setattr__(self, "_clarigrid_meta", dict(other._clarigrid_meta))
        elif isinstance(other, dict):
            # pd.concat passes a dict of frames; take the first EnergyFrame found.
            for v in other.values():
                if isinstance(v, EnergyFrame) and v._clarigrid_meta:
                    object.__setattr__(self, "_clarigrid_meta", dict(v._clarigrid_meta))
                    break
        return self

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _set_meta(self, **kwargs) -> "EnergyFrame":
        """Set metadata key-value pairs on this frame (mutates in place).

        Also updates ``df.attrs`` for backward compatibility.
        Returns ``self`` for chaining.
        """
        meta = dict(getattr(self, "_clarigrid_meta", {}))
        meta.update({k: v for k, v in kwargs.items() if v is not None})
        object.__setattr__(self, "_clarigrid_meta", meta)
        self.attrs.update(meta)
        return self

    # ------------------------------------------------------------------
    # Public metadata properties
    # ------------------------------------------------------------------

    @property
    def currency(self) -> str | None:
        """ISO currency code for price data (``"EUR"`` or ``"GBP"``)."""
        return self._clarigrid_meta.get("currency")

    @property
    def provider(self) -> str | None:
        """Registry name of the source provider."""
        return self._clarigrid_meta.get("provider")

    @property
    def zone(self) -> str | None:
        """Bidding zone / location identifier used for this request."""
        return self._clarigrid_meta.get("zone")

    @property
    def dataset(self) -> str | None:
        """Dataset type (``"prices"``, ``"load"``, ``"generation"`` …)."""
        return self._clarigrid_meta.get("dataset")

    @property
    def fetched_at(self) -> str | None:
        """ISO-8601 UTC timestamp when the data was fetched from the provider."""
        return self._clarigrid_meta.get("fetched_at")


# ── Price column → currency map ────────────────────────────────────────────

_PRICE_COL_MAP: dict[str, str] = {
    "price_gbp_mwh": "GBP",
    "price_eur_mwh": "EUR",
    COLUMN_PRICE:    "EUR",   # already canonical; assume EUR if unset
}


# ── Internal helpers ───────────────────────────────────────────────────────

def _infer_interval_minutes(df: pd.DataFrame) -> float:
    """Infer the data interval in minutes from the DatetimeIndex.

    Returns 60.0 (hourly) when the interval cannot be determined.
    """
    if len(df) < 2:
        return 60.0
    if df.index.freq is not None:
        return df.index.freq.nanos / 1e9 / 60.0
    diffs = pd.Series(df.index).diff().dropna()
    if diffs.empty:
        return 60.0
    median_s = diffs.median().total_seconds()
    return median_s / 60.0 if median_s > 0 else 60.0


def _to_energy_frame(df: pd.DataFrame) -> EnergyFrame:
    """Wrap a plain DataFrame as EnergyFrame, preserving any existing attrs."""
    if isinstance(df, EnergyFrame):
        return df
    result = EnergyFrame(df)
    # Carry over attrs set upstream (e.g. currency restored from cache sidecar).
    if df.attrs:
        result._set_meta(**{k: v for k, v in df.attrs.items()
                            if isinstance(v, (str, int, float, bool))})
    return result


# ── Normalisation functions ────────────────────────────────────────────────

def normalise_prices(df: pd.DataFrame) -> EnergyFrame:
    """Rename any recognised price column to ``price_mwh``.

    Sets ``EnergyFrame.currency`` (and ``df.attrs["currency"]``) to the ISO
    currency code.  Existing ``df.attrs["currency"]`` takes precedence so that
    cache-restored data with GBP currency is not incorrectly mapped to EUR.

    Returns an EnergyFrame regardless of whether a price column was found.
    """
    result = _to_energy_frame(df)
    if result.empty:
        return result

    for src_col, default_currency in _PRICE_COL_MAP.items():
        if src_col in result.columns:
            if src_col != COLUMN_PRICE:
                result = EnergyFrame(result.rename(columns={src_col: COLUMN_PRICE}))
            # Existing currency (e.g. from cache sidecar) takes precedence.
            currency = result.attrs.get("currency") or default_currency
            result._set_meta(currency=currency)
            return result

    return result


def normalise_load(df: pd.DataFrame) -> EnergyFrame:
    """Ensure the load column is named ``load_mw``.

    No-op when the column already exists.  Raises ``ProviderError`` if no
    recognisable load column is found rather than silently renaming an
    unrelated numeric column.
    """
    from clarigrid.core.exceptions import ProviderError

    result = _to_energy_frame(df)
    if result.empty or COLUMN_LOAD in result.columns:
        return result

    candidates = [
        c for c in result.columns
        if "load" in c.lower() or "demand" in c.lower()
    ]
    if not candidates:
        raise ProviderError(
            f"Provider did not return a recognisable load column. "
            f"Expected '{COLUMN_LOAD}' or a column containing 'load'/'demand'. "
            f"Got: {list(result.columns)}"
        )
    return EnergyFrame(result.rename(columns={candidates[0]: COLUMN_LOAD}))


def normalise_generation(df: pd.DataFrame) -> EnergyFrame:
    """Normalise generation column names and units.

    - Strips any ``columns.name`` metadata (artefact of ``pivot_table``).
    - Converts columns with ``_mwh`` suffix to ``_mw``, scaling by the data
      interval so the result is always **average power in MW**.

      For hourly data the scale factor is 1.0 (MWh/h = MW).
      For 15-minute data the scale factor is 4.0 (MWh per 15 min × 4 = MW).

    All generation data is stored internally in MW.
    """
    result = _to_energy_frame(df)
    if result.empty:
        result.columns.name = None
        return result

    # Work on a copy to avoid mutating the provider's DataFrame.
    data = pd.DataFrame(result).copy()
    data.columns.name = None

    mwh_cols = [c for c in data.columns if isinstance(c, str) and c.endswith("_mwh")]
    if mwh_cols:
        interval_min = _infer_interval_minutes(data)
        scale = 60.0 / interval_min   # 1.0 hourly, 4.0 for 15-min, 2.0 for 30-min …
        if scale != 1.0:
            data[mwh_cols] = data[mwh_cols] * scale
        rename = {col: col[:-4] + "_mw" for col in mwh_cols}
        data = data.rename(columns=rename)

    out = EnergyFrame(data)
    out._set_meta(**result._clarigrid_meta)
    return out
