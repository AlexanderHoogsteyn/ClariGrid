"""Elia Open Data provider — Belgium electricity.

No API key required. Uses the Opendatasoft API.
Self-registers on import as ``"elia"``.

Usage::

    import clarigrid as cg
    cg.connect("elia")
    df = cg.get_load("BE", "2025-01-01", "2025-01-07")
    df = cg.get_generation("BE", "2025-01-01", "2025-01-07")

Notes:
    Day-ahead prices are not published by Elia. Use ENTSO-E (requires API key)
    or SMARD (Germany only) for prices.
    Data is 15-minute resolution.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from clarigrid.core.http import get_json
from clarigrid.core.interface import DataProvider
from clarigrid.core.registry import register_provider
from clarigrid.core.types import COLUMN_LOAD, STANDARD_TZ
from clarigrid.utils.time import normalise_index

_BASE = "https://opendata.elia.be/api/explore/v2.1/catalog/datasets"
_PAGE_SIZE = 100  # ODS hard max per page

# Dataset IDs (verified against live API May 2026)
_DS_LOAD = "ods003"              # Total load — field: eliagridload
_DS_SOLAR = "ods032"            # Solar/PV by region — field: measured, sum across regions
_DS_WIND = "ods031"             # Wind by region & onshore/offshore — field: measured
_DS_GENERATION_MIX = "ods033"  # Generation mix by fuelcode — field: generatedpower

# Fuel code → standard column name
_FUEL_RENAME: dict[str, str] = {
    "NG": "gas_mw",
    "NU": "nuclear_mw",
    "CP": "pump_storage_mw",
    "LF": "hydro_mw",
    "WA": "water_mw",
    "WI": "wind_integrated_mw",
    "Other": "other_mw",
}


def _date_where(start: str, end: str) -> str:
    s = pd.Timestamp(start).date()
    e = pd.Timestamp(end).date()
    return f"datetime >= '{s}' AND datetime <= '{e}'"


def _fetch_all(dataset_id: str, where: str, select: str | None = None) -> list[dict[str, Any]]:
    """Paginate through all records for an Elia ODS dataset."""
    url = f"{_BASE}/{dataset_id}/records"
    params: dict[str, Any] = {
        "where": where,
        "limit": _PAGE_SIZE,
        "order_by": "datetime asc",
        "timezone": "UTC",
        "offset": 0,
    }
    if select:
        params["select"] = select
    records: list[dict] = []
    while True:
        data = get_json(url, params)
        page = data.get("results", [])
        records.extend(page)
        total = data.get("total_count", 0)
        params["offset"] += _PAGE_SIZE
        if params["offset"] >= total:
            break
    return records


def _to_ts_series(records: list[dict], time_field: str, value_field: str) -> pd.Series:
    """Convert a flat list of records to a float Series indexed by UTC timestamp."""
    rows = {}
    for r in records:
        try:
            ts = pd.Timestamp(r[time_field], tz="UTC")
            val = float(r[value_field]) if r.get(value_field) is not None else float("nan")
            rows[ts] = rows.get(ts, 0.0) + val  # sum across regions / types
        except (KeyError, ValueError, TypeError):
            continue
    if not rows:
        return pd.Series(dtype=float)
    s = pd.Series(rows)
    s.index = pd.DatetimeIndex(s.index)
    s.index.name = "utc_time"
    return s.sort_index()


class EliaProvider(DataProvider):
    """Elia Open Data — Belgium electricity."""

    def get_prices(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        raise NotImplementedError(
            "Elia does not publish day-ahead electricity prices. "
            "Use the ENTSO-E provider (cg.connect('entsoe')) or SMARD for DE prices."
        )

    def get_load(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        records = _fetch_all(_DS_LOAD, _date_where(start, end), select="datetime,eliagridload")
        s = _to_ts_series(records, "datetime", "eliagridload")
        if s.empty:
            return pd.DataFrame()
        df = s.rename(COLUMN_LOAD).to_frame()
        return normalise_index(df, STANDARD_TZ)

    def get_generation(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        where = _date_where(start, end)
        frames: dict[str, pd.Series] = {}

        # --- Solar PV (sum across regions) ---
        solar_recs = _fetch_all(_DS_SOLAR, where, select="datetime,measured")
        s_solar = _to_ts_series(solar_recs, "datetime", "measured")
        if not s_solar.empty:
            frames["solar_mw"] = s_solar

        # --- Wind (split offshore/onshore, sum across regions) ---
        wind_recs = _fetch_all(_DS_WIND, where, select="datetime,offshoreonshore,measured")
        onshore_rows: dict = {}
        offshore_rows: dict = {}
        for r in wind_recs:
            try:
                ts = pd.Timestamp(r["datetime"], tz="UTC")
                val = float(r.get("measured") or 0)
                oo = r.get("offshoreonshore", "").lower()
                if "offshore" in oo:
                    offshore_rows[ts] = offshore_rows.get(ts, 0.0) + val
                else:
                    onshore_rows[ts] = onshore_rows.get(ts, 0.0) + val
            except (KeyError, ValueError, TypeError):
                continue
        if onshore_rows:
            s = pd.Series(onshore_rows)
            s.index = pd.DatetimeIndex(s.index)
            s.index.name = "utc_time"
            frames["wind_onshore_mw"] = s.sort_index()
        if offshore_rows:
            s = pd.Series(offshore_rows)
            s.index = pd.DatetimeIndex(s.index)
            s.index.name = "utc_time"
            frames["wind_offshore_mw"] = s.sort_index()

        # --- Generation mix (thermal, nuclear, etc.) ---
        mix_recs = _fetch_all(_DS_GENERATION_MIX, where, select="datetime,fuelcode,generatedpower")
        fuel_data: dict[str, dict] = {}
        for r in mix_recs:
            try:
                ts = pd.Timestamp(r["datetime"], tz="UTC")
                fuel = _FUEL_RENAME.get(r.get("fuelcode", ""), None)
                if fuel is None:
                    continue
                val = float(r.get("generatedpower") or 0)
                fuel_data.setdefault(fuel, {})
                fuel_data[fuel][ts] = fuel_data[fuel].get(ts, 0.0) + val
            except (KeyError, ValueError, TypeError):
                continue
        for fuel, ts_vals in fuel_data.items():
            if ts_vals:
                s = pd.Series(ts_vals)
                s.index = pd.DatetimeIndex(s.index)
                s.index.name = "utc_time"
                frames[fuel] = s.sort_index()

        if not frames:
            return pd.DataFrame()

        result = pd.DataFrame(frames)
        return normalise_index(result, STANDARD_TZ)

    def zones(self) -> set[str]:
        return {"BE"}

    def capabilities(self) -> set[str]:
        return {"load", "generation"}

    def name(self) -> str:
        return "Elia Open Data (Belgium)"


def register() -> None:
    """Register the Elia provider. Called automatically on import."""
    register_provider("elia", EliaProvider())


register()
