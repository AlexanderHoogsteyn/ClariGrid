"""TenneT NL provider — Netherlands electricity imbalance data.

Requires a free API key from https://developer.tennet.eu/register/
Key stored as ``TENNET_API_KEY`` in ``~/.config/clarigrid/.env``,
or via a clarigrid.energy account (preferred).

Self-registers on import as ``"tennet"``.

Usage::

    import clarigrid as cg
    cg.connect("tennet")
    df = cg.get_prices("NL", "2026-05-01", "2026-05-07")  # imbalance settlement prices
    df = cg.get_load("NL",   "2026-05-01", "2026-05-07")  # metered infeed (MW)

Notes
-----
- ``get_prices()`` returns **imbalance settlement prices**, not day-ahead prices.
  Primary column is ``price_mwh`` (upward dispatch price).  Extra column
  ``price_down_mwh`` holds the downward dispatch price.
- ``get_load()`` returns **metered infeed** in MW (converted from MWh/15-min PTU).
- Time resolution is 15-minute PTU (Programme Time Unit).
- Zone parameter is always ``"NL"`` — TenneT NL has no sub-zones.
- The legacy tennet.org XML API was decommissioned in December 2024.

API reference: https://developer.tennet.eu/specs/
"""

from __future__ import annotations

import io

import pandas as pd

from clarigrid.core.http import get_text
from clarigrid.core.interface import DataProvider
from clarigrid.core.registry import register_provider
from clarigrid.core.types import COLUMN_LOAD, STANDARD_TZ
from clarigrid.utils.time import normalise_index

_BASE = "https://api.tennet.eu/publications/v1"
_TZ = "Europe/Amsterdam"

# PTU resolution in minutes — all TenneT v1 endpoints are 15-min.
_PTU_MINUTES = 15
# 1 MWh per PTU → MW: 1 MWh / (15 min / 60 min/h) = 4 MW
_MWH_PER_PTU_TO_MW = 60 / _PTU_MINUTES


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Accept": "text/csv",
        "apikey": api_key,  # lowercase — confirmed against live API
    }


def _date_param(ts: str | pd.Timestamp) -> str:
    """Format timestamp to DD-MM-YYYY HH:MM:SS as required by TenneT API."""
    return pd.Timestamp(ts).strftime("%d-%m-%Y %H:%M:%S")


def _fetch_csv_single(
    endpoint: str,
    date_from: pd.Timestamp,
    date_to: pd.Timestamp,
    api_key: str,
) -> pd.DataFrame:
    """Single request for one chunk.  Returns empty DataFrame on no data."""
    params = {
        "date_from": _date_param(date_from),
        "date_to": _date_param(date_to),
    }
    text = get_text(f"{_BASE}/{endpoint}", params=params, headers=_headers(api_key))
    if not text.strip():
        return pd.DataFrame()
    return pd.read_csv(io.StringIO(text))


def _fetch_csv(
    endpoint: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    api_key: str,
    max_chunk_days: int = 30,
) -> pd.DataFrame:
    """GET *endpoint* for [start, end], chunking into *max_chunk_days* windows.

    TenneT enforces per-endpoint range limits:
      - settlement-prices      : ~30 days
      - metered-injections     : 1 day
      - merit-order-list       : ~30 days (assumed)
      - settled-imbalance-volumes : ~30 days (assumed)

    Chunks are fetched sequentially and concatenated.
    """
    t_start = pd.Timestamp(start).normalize()
    # date_to is exclusive-end: add 1 day so the full end-date is included.
    t_end = pd.Timestamp(end).normalize() + pd.Timedelta(days=1)

    chunks: list[pd.DataFrame] = []
    cursor = t_start
    while cursor < t_end:
        chunk_end = min(cursor + pd.Timedelta(days=max_chunk_days), t_end)
        chunk = _fetch_csv_single(endpoint, cursor, chunk_end, api_key)
        if not chunk.empty:
            chunks.append(chunk)
        cursor = chunk_end

    if not chunks:
        return pd.DataFrame()
    result = pd.concat(chunks, ignore_index=True)
    # Drop duplicate rows that may appear at chunk boundaries.
    return result.drop_duplicates(subset=["Timeinterval Start Loc", "Isp"])


def _parse_index(df: pd.DataFrame) -> pd.DatetimeIndex:
    """Convert 'Timeinterval Start Loc' column to UTC DatetimeIndex.

    TenneT timestamps are Amsterdam local time (ISO 8601, no tz suffix).
    """
    raw = pd.to_datetime(df["Timeinterval Start Loc"])
    # tz_localize handles DST transitions via nonexistent/ambiguous shifts.
    aware = raw.dt.tz_localize(_TZ, ambiguous="infer", nonexistent="shift_forward")
    return aware.dt.tz_convert("UTC")


class TennetProvider(DataProvider):
    """TenneT NL — Netherlands imbalance settlement prices and metered infeed."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    # ── DataProvider interface ─────────────────────────────────────────────

    def name(self) -> str:
        return "TenneT NL (developer.tennet.eu)"

    def zones(self) -> set[str]:
        return {"NL"}

    def capabilities(self) -> set[str]:
        return {"prices", "load"}

    # ── Data methods ───────────────────────────────────────────────────────

    def get_prices(
        self,
        zone: str,
        start: str | pd.Timestamp,
        end: str | pd.Timestamp,
        **kwargs,
    ) -> pd.DataFrame:
        """Return imbalance settlement prices (15-min PTU) in EUR/MWh.

        Columns
        -------
        ``price_mwh``
            Upward dispatch price — price paid for activated upward regulation bids.
            This is the primary imbalance price (feeds into ``df.attrs["currency"]``).
        ``price_down_mwh``
            Downward dispatch price — price paid for activated downward bids.
        ``price_shortage_mwh``
            Imbalance price applied to BRPs with a shortage (positive imbalance).
        ``price_surplus_mwh``
            Imbalance price applied to BRPs with a surplus (negative imbalance).
        ``incident_reserve_up``, ``incident_reserve_down``
            Boolean flags: True if incident reserve was activated this PTU.
        """
        raw = _fetch_csv("settlement-prices", start, end, self._api_key, max_chunk_days=30)
        if raw.empty:
            return pd.DataFrame()

        raw.columns = raw.columns.str.strip()
        raw.index = _parse_index(raw)
        raw.index.name = "utc_time"

        df = pd.DataFrame(index=raw.index)
        df["price_mwh"] = pd.to_numeric(raw["Price Dispatch Up"], errors="coerce")
        df["price_down_mwh"] = pd.to_numeric(raw["Price Dispatch Down"], errors="coerce")
        df["price_shortage_mwh"] = pd.to_numeric(raw["Price Shortage"], errors="coerce")
        df["price_surplus_mwh"] = pd.to_numeric(raw["Price Surplus"], errors="coerce")
        df["incident_reserve_up"] = raw["Incident Reserve Up"].str.upper() == "YES"
        df["incident_reserve_down"] = raw["Incident Reserve Down"].str.upper() == "YES"

        df.attrs["currency"] = "EUR"
        df.attrs["price_type"] = "imbalance_settlement"
        return normalise_index(df, STANDARD_TZ)

    def get_load(
        self,
        zone: str,
        start: str | pd.Timestamp,
        end: str | pd.Timestamp,
        **kwargs,
    ) -> pd.DataFrame:
        """Return metered infeed in MW (15-min PTU resolution).

        Source: ``/metered-injections`` — net infeed observed on the NL
        transmission network (feed-in minus exports plus imports).

        Column: ``load_mw``
        """
        raw = _fetch_csv("metered-injections", start, end, self._api_key, max_chunk_days=1)
        if raw.empty:
            return pd.DataFrame()

        raw.columns = raw.columns.str.strip()
        raw.index = _parse_index(raw)
        raw.index.name = "utc_time"

        df = pd.DataFrame(index=raw.index)
        # API returns MWh per 15-min PTU → convert to MW (average power).
        mwh = pd.to_numeric(raw["Measured Infeed"], errors="coerce")
        df[COLUMN_LOAD] = mwh * _MWH_PER_PTU_TO_MW

        return normalise_index(df, STANDARD_TZ)

    def get_generation(self, zone: str, start: str | pd.Timestamp,
                       end: str | pd.Timestamp, **kwargs) -> pd.DataFrame:
        raise NotImplementedError(
            "TenneT NL does not publish generation mix data. "
            "Use cg.connect('entsoe') for NL generation per fuel type."
        )


# ── Registration ───────────────────────────────────────────────────────────

def register() -> None:
    """Register TenneT provider if key is available, otherwise skip silently."""
    from clarigrid._keystore import get_key
    key = get_key("TENNET_API_KEY")
    if key:
        register_provider("tennet", TennetProvider(key))
    # No stub registered — connect("tennet") will trigger auth flow via _auth.py


register()
