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

import re
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

# Additional dataset IDs (Elia Open Data catalogue).
# NOTE: Elia split most balancing/imbalance series at 22/05/2024 (MARI/PICASSO
# go-live).  The IDs below are the "as of 22/05/2024" variants, valid for the
# current/recent windows the SDK targets.  Pre-2024 history lives in separate
# legacy datasets (ods047/ods064/...) and is intentionally not wired here.
_DS_IMBALANCE_PRICES = "ods134"     # Imbalance prices per quarter-hour (as of 22/05/2024)
_DS_SYSTEM_IMBALANCE = "ods126"     # Current system imbalance (post-MARI, 1 min)
_DS_BALANCING_VOLUMES = "ods132"    # Activated balancing volumes BE / quarter-hour
_DS_BALANCING_PRICES = "ods153"     # Available balancing energy prices / quarter-hour
_DS_WIND_FORECAST = "ods031"        # Wind production estimation & forecast (historical)
_DS_SOLAR_FORECAST = "ods032"       # PV production estimation & forecast (historical)
_DS_LOAD_FORECAST = "ods001"        # Measured + forecast total load on the Belgian grid
_DS_PHYSICAL_FLOWS = "ods026"       # Cross-border physical flow per border
_DS_COMMERCIAL_SCHEDULE = "ods015"  # Day-ahead commercial schedule per border
_DS_NTC = "ods008"                  # Week-ahead forecast NTC per border
_DS_NET_POSITION = "ods023"         # Day-ahead implicit net position
_DS_CO2_INTENSITY = "ods192"        # Production- & consumption-based CO2 intensity

# Metadata fields present across many ODS datasets that are NOT data dimensions.
# Dropped before shape detection so they neither pollute numeric columns nor
# get mistaken for a pivot category (e.g. constant ``resolutioncode='PT15M'``).
_META_FIELDS: frozenset[str] = frozenset({
    "resolutioncode", "qualitystatus", "gridconnectiontype",
    "decrementalbidid", "region", "offshoreonshore", "dayofweek",
})

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


def _snake(value: Any) -> str:
    """Lowercase snake_case a label, collapsing non-alphanumerics."""
    out = re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower())
    return out.strip("_") or "value"


def _auto_frame(
    records: list[dict[str, Any]],
    *,
    time_field: str = "datetime",
    value_suffix: str = "",
    rename: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Generic ODS records → tidy UTC-indexed DataFrame.

    Robust to schemas whose exact field names we do not hard-code:

    - One categorical + exactly one numeric field → pivot the numeric value
      into one column per category (combining multiple categorical fields).
    - Otherwise → keep all numeric fields as columns, summed per timestamp.

    Numeric columns are snake_cased; ``value_suffix`` is appended to pivoted
    columns (e.g. ``"_mw"``).  ``rename`` applies canonical names last.
    """
    if not records:
        return pd.DataFrame()

    df = pd.DataFrame.from_records(records)
    if time_field not in df.columns:
        return pd.DataFrame()

    df[time_field] = pd.to_datetime(df[time_field], utc=True, errors="coerce")
    df = df.dropna(subset=[time_field])
    if df.empty:
        return pd.DataFrame()

    other = [c for c in df.columns if c != time_field and c not in _META_FIELDS]
    numeric: dict[str, pd.Series] = {}
    categorical: list[str] = []
    for c in other:
        coerced = pd.to_numeric(df[c], errors="coerce")
        if coerced.notna().any():
            numeric[c] = coerced
        else:
            categorical.append(c)

    if not numeric:
        return pd.DataFrame()

    if categorical and len(numeric) == 1:
        val_name = next(iter(numeric))
        work = df[[time_field] + categorical].copy()
        work[val_name] = numeric[val_name].values
        if len(categorical) > 1:
            key = work[categorical].astype(str).agg("_".join, axis=1)
        else:
            key = work[categorical[0]].astype(str)
        work["_key"] = key.map(_snake)
        out = work.pivot_table(
            index=time_field, columns="_key", values=val_name, aggfunc="sum"
        )
        out.columns = [f"{c}{value_suffix}" for c in out.columns]
        out.columns.name = None
    else:
        wide = pd.DataFrame(numeric)
        wide[time_field] = df[time_field].values
        out = wide.groupby(time_field).sum(min_count=1)
        out.columns = [_snake(c) for c in out.columns]

    out = out.sort_index()
    out.index.name = "utc_time"
    if rename:
        out = out.rename(columns={k: v for k, v in rename.items() if k in out.columns})
    return normalise_index(out, STANDARD_TZ)


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

    # ── Forecasts ───────────────────────────────────────────────────────
    def get_load_forecast(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        """Measured + day-ahead + week-ahead total load forecast (MW)."""
        recs = _fetch_all(_DS_LOAD_FORECAST, _date_where(start, end))
        return _auto_frame(recs)

    def get_generation_forecast(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        """Wind + solar production forecast (MW), summed across regions.

        Columns are prefixed ``wind_`` / ``solar_`` and retain the underlying
        forecast horizon field names (intraday, day-ahead, week-ahead, P10/P90).
        """
        where = _date_where(start, end)
        frames: list[pd.DataFrame] = []
        wind = _auto_frame(_fetch_all(_DS_WIND_FORECAST, where))
        if not wind.empty:
            frames.append(wind.add_prefix("wind_"))
        solar = _auto_frame(_fetch_all(_DS_SOLAR_FORECAST, where))
        if not solar.empty:
            frames.append(solar.add_prefix("solar_"))
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, axis=1).sort_index()
        out.index.name = "utc_time"
        return normalise_index(out, STANDARD_TZ)

    # ── Imbalance & balancing ───────────────────────────────────────────
    def get_imbalance_prices(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        """Quarter-hour imbalance prices (EUR/MWh) and components (ods134)."""
        recs = _fetch_all(_DS_IMBALANCE_PRICES, _date_where(start, end))
        return _auto_frame(recs)

    def get_system_imbalance(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        """System imbalance (SI) and balancing component volumes in MW (ods126)."""
        recs = _fetch_all(_DS_SYSTEM_IMBALANCE, _date_where(start, end))
        return _auto_frame(
            recs,
            rename={
                "systemimbalance": "system_imbalance_mw",
                "ace": "ace_mw",
                "igccvolumeup": "igcc_up_mw",
                "igccvolumedown": "igcc_down_mw",
                "afrrvolumeup": "afrr_up_mw",
                "afrrvolumedown": "afrr_down_mw",
                "mfrrsaup": "mfrr_sa_up_mw",
                "mfrrsadown": "mfrr_sa_down_mw",
                "mfrrdaup": "mfrr_da_up_mw",
                "mfrrdadown": "mfrr_da_down_mw",
                "reserve_sharing_import": "reserve_sharing_import_mw",
                "reserve_sharing_export": "reserve_sharing_export_mw",
            },
        )

    def get_balancing_volumes(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        """Activated balancing energy volumes per product (MW) per quarter-hour."""
        recs = _fetch_all(_DS_BALANCING_VOLUMES, _date_where(start, end))
        return _auto_frame(recs, value_suffix="_mw")

    def get_balancing_prices(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        """Activated balancing energy prices per product (EUR/MWh) per quarter-hour."""
        recs = _fetch_all(_DS_BALANCING_PRICES, _date_where(start, end))
        return _auto_frame(recs, value_suffix="_eur_mwh")

    # ── Cross-border & capacity ─────────────────────────────────────────
    def get_physical_flows(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        """Cross-border physical flows per border (MW), columns ``flow_<border>_mw``."""
        recs = _fetch_all(_DS_PHYSICAL_FLOWS, _date_where(start, end))
        df = _auto_frame(recs, value_suffix="_mw")
        return df.add_prefix("flow_") if not df.empty else df

    def get_commercial_schedule(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        """Day-ahead commercial schedule per border (MW)."""
        recs = _fetch_all(_DS_COMMERCIAL_SCHEDULE, _date_where(start, end))
        df = _auto_frame(recs, value_suffix="_mw")
        return df.add_prefix("schedule_") if not df.empty else df

    def get_ntc(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        """Week-ahead net transfer capacity per border (MW)."""
        recs = _fetch_all(_DS_NTC, _date_where(start, end))
        df = _auto_frame(recs, value_suffix="_mw")
        return df.add_prefix("ntc_") if not df.empty else df

    def get_net_position(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        """Day-ahead implicit net position (MW; exports +, imports −)."""
        recs = _fetch_all(_DS_NET_POSITION, _date_where(start, end))
        return _auto_frame(recs, rename={"implicitnetposition": "net_position_mw"})

    # ── Environmental ───────────────────────────────────────────────────
    def get_co2_intensity(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        """Production- and consumption-based CO2 intensity (gCO2eq/kWh)."""
        recs = _fetch_all(_DS_CO2_INTENSITY, _date_where(start, end))
        return _auto_frame(
            recs,
            rename={
                "production": "co2_production_g_kwh",
                "consumption": "co2_consumption_g_kwh",
                "productionbasedco2intensity": "co2_production_g_kwh",
                "consumptionbasedco2intensity": "co2_consumption_g_kwh",
            },
        )

    # ── Declarations ────────────────────────────────────────────────────
    def zones(self) -> set[str]:
        return {"BE"}

    def capabilities(self) -> set[str]:
        return {
            "load", "generation",
            "load_forecast", "generation_forecast",
            "imbalance_prices", "system_imbalance",
            "balancing_volumes", "balancing_prices",
            "physical_flows", "commercial_schedule", "ntc",
            "net_position", "co2_intensity",
        }

    def name(self) -> str:
        return "Elia Open Data (Belgium)"


def register() -> None:
    """Register the Elia provider. Called automatically on import."""
    register_provider("elia", EliaProvider())


register()
