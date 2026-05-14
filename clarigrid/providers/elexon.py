"""Elexon Insights Solution (BMRS) provider — Great Britain balancing mechanism.

No API key required. Uses the new BMRS REST API (post-May 2024).
Self-registers on import as ``"elexon"``.

Usage::

    import clarigrid as cg
    cg.connect("elexon")
    df = cg.get_prices("GB", "2025-01-01", "2025-01-07")   # SBP / SSP
    df = cg.get_generation("GB", "2025-01-01", "2025-01-07")  # by fuel type

Notes:
    - ``zone`` is ignored — data is always GB-wide.
    - ``get_prices`` returns both system sell price (SSP) and system buy price
      (SBP) as separate columns rather than a single ``price_eur_mwh``.
    - Settlement periods are converted to UTC automatically.

API spec: https://data.elexon.co.uk/bmrs/api/v1/swagger/ui
"""

from __future__ import annotations

import pandas as pd

from clarigrid.core.http import paginate_pages
from clarigrid.core.interface import DataProvider
from clarigrid.core.registry import register_provider
from clarigrid.core.types import STANDARD_TZ
from clarigrid.utils.time import normalise_index

_BASE = "https://data.elexon.co.uk/bmrs/api/v1"

# Elexon uses GB settlement periods (30-min, Europe/London time), same as NESO.
# Settlement period start = date 00:00 local + (sp - 1) * 30 min.


def _sp_to_utc(date_str: str, sp: int | str) -> pd.Timestamp:
    local_dt = pd.Timestamp(date_str) + pd.Timedelta(minutes=30 * (int(sp) - 1))
    return local_dt.tz_localize(
        "Europe/London", ambiguous=False, nonexistent="shift_forward"
    ).tz_convert("UTC")


def _fetch_raw(endpoint: str, params: dict) -> list[dict]:
    """Fetch an endpoint that returns a top-level JSON list (no pagination wrapper)."""
    from clarigrid.core.http import get_json
    result = get_json(f"{_BASE}/{endpoint}", params)
    if isinstance(result, list):
        return result
    return result.get("data", [])


def _fetch_data(endpoint: str, params: dict) -> list[dict]:
    """Paginate through an Elexon BMRS endpoint."""
    records: list[dict] = []
    for page in paginate_pages(
        f"{_BASE}/{endpoint}",
        params,
        data_key="data",
        page_param="page",
        size_param="pageSize",
        page_size=500,
        throttle=0.2,
    ):
        records.extend(page)
    return records


class ElexonProvider(DataProvider):
    """Elexon Insights Solution (BMRS) — Great Britain balancing."""

    def get_prices(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        """Return GB market index price in GBP/MWh per settlement period.

        Source: ``/balancing/pricing/market-index`` (APXMIDP provider).
        Column returned: ``price_gbp_mwh`` (not EUR/MWh — no FX conversion applied).

        Multiple data providers may appear per SP; this method returns the
        APXMIDP value (main UK market index) when available, else first value.
        """
        params = {
            "from": pd.Timestamp(start).strftime("%Y-%m-%d"),
            "to": pd.Timestamp(end).strftime("%Y-%m-%d"),
            "format": "json",
        }
        records = _fetch_data("balancing/pricing/market-index", params)

        if not records:
            return pd.DataFrame()

        # Group by settlement period; prefer APXMIDP provider.
        by_sp: dict[pd.Timestamp, float] = {}
        for r in records:
            try:
                if "startTime" in r and r["startTime"]:
                    ts = pd.Timestamp(r["startTime"]).tz_convert("UTC")
                else:
                    ts = _sp_to_utc(r["settlementDate"], r["settlementPeriod"])
                price = float(r.get("price") or float("nan"))
                provider = r.get("dataProvider", "")
                # Overwrite with APXMIDP if present; otherwise keep first.
                if ts not in by_sp or "APXMIDP" in provider.upper():
                    by_sp[ts] = price
            except (KeyError, ValueError, TypeError):
                continue

        if not by_sp:
            return pd.DataFrame()

        df = pd.Series(by_sp, name="price_gbp_mwh").sort_index().to_frame()
        df.index.name = "utc_time"
        return normalise_index(df, STANDARD_TZ)

    def get_load(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        raise NotImplementedError(
            "Elexon BMRS does not expose a simple demand endpoint. "
            "Use the 'neso' provider for GB national demand."
        )

    def get_generation(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        """Return actual generation by fuel type in MW.

        Source: ``/generation/outturn/summary`` (B1630).

        **Note:** This endpoint provides a rolling window of ~24 h of recent
        data. Historical date ranges are not supported by the Elexon API for
        this endpoint — passing historical ``start``/``end`` will return the
        most recently published data instead. For historical GB fuel mix data,
        use the ``Elexon BMRS historical CSV files`` or the GridWatch API.
        """
        params = {
            "from": pd.Timestamp(start).strftime("%Y-%m-%dT%H:%M"),
            "to": pd.Timestamp(end).strftime("%Y-%m-%dT%H:%M"),
            "format": "json",
        }
        # Response is a list of {startTime, settlementPeriod, data: [{fuelType, generation}]}
        raw = _fetch_raw("generation/outturn/summary", params)

        if not raw:
            return pd.DataFrame()

        rows: list[dict] = []
        for sp_rec in raw:
            try:
                ts = pd.Timestamp(sp_rec["startTime"]).tz_convert("UTC")
                for fuel_rec in sp_rec.get("data", []):
                    fuel = str(fuel_rec.get("fuelType", "other")).lower() + "_mw"
                    gen = float(fuel_rec.get("generation") or 0)
                    rows.append({"utc_time": ts, "fuel_type": fuel, "generation_mw": gen})
            except (KeyError, ValueError, TypeError):
                continue

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        pivot = df.pivot_table(
            index="utc_time", columns="fuel_type", values="generation_mw", aggfunc="sum"
        )
        pivot.index.name = "utc_time"
        pivot.columns.name = None
        return normalise_index(pivot, STANDARD_TZ)

    def capabilities(self) -> set[str]:
        return {"prices", "generation"}

    def name(self) -> str:
        return "Elexon Insights Solution (BMRS)"


def register() -> None:
    """Register the Elexon provider. Called automatically on import."""
    register_provider("elexon", ElexonProvider())


register()
