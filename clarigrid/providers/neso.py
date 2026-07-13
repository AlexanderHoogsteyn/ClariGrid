"""NESO (National Energy System Operator) Data Portal provider — Great Britain.

No API key required. Uses the CKAN open data API.
Self-registers on import as ``"neso"``.

Covers:
    - GB electricity demand (``get_load``): ``TSD`` (Transmission System Demand, MW)
    - GB embedded generation (``get_generation``): embedded wind + solar MW
    - Full BM-connected generation mix: use the ``"elexon"`` provider instead.

Usage::

    import clarigrid as cg
    cg.connect("neso")
    df = cg.get_load("GB", "2025-01-01", "2025-01-07")

Notes:
    - Data is per settlement period (30-min in Europe/London time). Converted
      to UTC automatically.
    - Demand data is split by year. The provider fetches the correct year
      resource(s) dynamically and caches them for the session.
    - ``zone`` is ignored — data is always GB-wide.
"""

from __future__ import annotations

import time

import pandas as pd

from clarigrid.core.http import get_json
from clarigrid.core.interface import DataProvider
from clarigrid.core.registry import register_provider
from clarigrid.core.types import COLUMN_LOAD, STANDARD_TZ
from clarigrid.utils.time import normalise_index, parse_dt

_CKAN = "https://api.neso.energy/api/3/action"

# NESO Historic Demand Data package — one resource per calendar year.
_DEMAND_PACKAGE = "historic-demand-data"

_CARBON_API = "https://api.carbonintensity.org.uk"
_CARBON_LICENSE = "CC BY 4.0"
_CARBON_MAX_WINDOW = pd.Timedelta(days=14)
_CARBON_CACHE_TTL_SECONDS = 60.0
_CARBON_CACHE: dict[tuple[str, str, str], tuple[float, list[dict]]] = {}


def _carbon_timestamp(value: str | pd.Timestamp) -> str:
    return parse_dt(value).strftime("%Y-%m-%dT%H:%MZ")


def _carbon_records(endpoint: str, start: str, end: str) -> list[dict]:
    """Fetch Carbon Intensity API data in bounded 14-day chunks."""
    cache_key = (endpoint, str(start), str(end))
    cached = _CARBON_CACHE.get(cache_key)
    if cached and time.monotonic() - cached[0] < _CARBON_CACHE_TTL_SECONDS:
        return [dict(row) for row in cached[1]]

    start_ts, end_ts = parse_dt(start), parse_dt(end)
    cursor = start_ts
    rows: list[dict] = []
    while cursor <= end_ts:
        chunk_end = min(cursor + _CARBON_MAX_WINDOW, end_ts)
        payload = get_json(
            f"{_CARBON_API}/{endpoint}/"
            f"{_carbon_timestamp(cursor)}/{_carbon_timestamp(chunk_end)}"
        )
        rows.extend(payload.get("data", []))
        if chunk_end >= end_ts:
            break
        cursor = chunk_end
    filtered = []
    for row in rows:
        timestamp = pd.to_datetime(row.get("from"), utc=True, errors="coerce")
        if pd.notna(timestamp) and start_ts <= timestamp < end_ts:
            filtered.append(row)
    _CARBON_CACHE[cache_key] = (time.monotonic(), filtered)
    return [dict(row) for row in filtered]


def _carbon_intensity_frame(records: list[dict], value: str) -> pd.DataFrame:
    rows = []
    for record in records:
        intensity = record.get("intensity", {})
        rows.append({
            "utc_time": pd.to_datetime(record.get("from"), utc=True),
            value: pd.to_numeric(intensity.get(value), errors="coerce"),
        })
    if not rows:
        return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name="utc_time"))
    frame = pd.DataFrame(rows).dropna(subset=["utc_time"]).set_index("utc_time")
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    return normalise_index(frame)


def _generation_share_frame(records: list[dict]) -> pd.DataFrame:
    rows = []
    for record in records:
        row = {"utc_time": pd.to_datetime(record.get("from"), utc=True)}
        for item in record.get("generationmix", []):
            fuel = str(item.get("fuel", "")).lower().replace(" ", "_")
            row[f"{fuel}_share_pct"] = pd.to_numeric(item.get("perc"), errors="coerce")
        rows.append(row)
    if not rows:
        return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name="utc_time"))
    frame = pd.DataFrame(rows).dropna(subset=["utc_time"]).set_index("utc_time")
    return normalise_index(frame.sort_index())


def _sp_to_utc(date_str: str, sp: int | str) -> pd.Timestamp:
    """Convert settlement date + period to UTC Timestamp.

    GB settlement periods are 30-min slots in Europe/London local time.
    SP 1 starts at 00:00 local, SP 2 at 00:30, ...
    """
    local_dt = pd.Timestamp(date_str) + pd.Timedelta(minutes=30 * (int(sp) - 1))
    return local_dt.tz_localize(
        "Europe/London", ambiguous=False, nonexistent="shift_forward"
    ).tz_convert("UTC")


def _get_year_resources() -> dict[int, str]:
    """Return {year: resource_id} for all active Historic Demand Data resources."""
    data = get_json(f"{_CKAN}/package_show", {"id": _DEMAND_PACKAGE})
    resources: dict[int, str] = {}
    for res in data.get("result", {}).get("resources", []):
        if not res.get("datastore_active", False):
            continue
        name = res.get("name", "")
        # Resource names are like "Historic Demand Data 2025"
        for part in name.split():
            if part.isdigit() and len(part) == 4:
                year = int(part)
                resources[year] = res["id"]
                break
    return resources


def _sql_demand(resource_id: str, start: str, end: str) -> list[dict]:
    """Query a demand resource for a date range via the CKAN SQL endpoint."""
    s = pd.Timestamp(start).date()
    e = pd.Timestamp(end).date()
    sql = (
        f'SELECT * FROM "{resource_id}" '
        f"WHERE \"SETTLEMENT_DATE\" >= '{s}' "
        f"AND \"SETTLEMENT_DATE\" <= '{e}' "
        f'ORDER BY "SETTLEMENT_DATE" ASC, "SETTLEMENT_PERIOD" ASC '
        f"LIMIT 10000"
    )
    data = get_json(f"{_CKAN}/datastore_search_sql", {"sql": sql})
    return data.get("result", {}).get("records", [])


class NesoProvider(DataProvider):
    """NESO Data Portal — Great Britain electricity demand and embedded generation."""

    def __init__(self) -> None:
        self._year_resources: dict[int, str] | None = None

    def _resources(self) -> dict[int, str]:
        if self._year_resources is None:
            self._year_resources = _get_year_resources()
        return self._year_resources

    def _fetch_records(self, start: str, end: str) -> list[dict]:
        """Fetch demand records across all years that overlap [start, end]."""
        s_year = pd.Timestamp(start).year
        e_year = pd.Timestamp(end).year
        resources = self._resources()
        records: list[dict] = []
        for year in range(s_year, e_year + 1):
            rid = resources.get(year)
            if rid is None:
                continue
            records.extend(_sql_demand(rid, start, end))
        return records

    def _records_to_df(
        self, records: list[dict], value_cols: list[str]
    ) -> pd.DataFrame:
        if not records:
            return pd.DataFrame()

        # Detect which value columns are present in the data.
        sample = records[0]
        present = [c for c in value_cols if c in sample]
        if not present:
            return pd.DataFrame()

        rows: list[dict] = []
        for r in records:
            try:
                ts = _sp_to_utc(r["SETTLEMENT_DATE"], r["SETTLEMENT_PERIOD"])
                row: dict = {"utc_time": ts}
                for col in present:
                    v = r.get(col)
                    row[col] = float(v) if v not in (None, "") else float("nan")
                rows.append(row)
            except (KeyError, ValueError, TypeError):
                continue

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows).set_index("utc_time")
        df.index.name = "utc_time"
        return df

    def get_prices(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        raise NotImplementedError(
            "NESO does not provide day-ahead electricity prices. "
            "Use the 'elexon' provider for GB system prices (SBP/SSP)."
        )

    def get_load(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        """Return transmission system demand (TSD) in MW."""
        records = self._fetch_records(start, end)
        df = self._records_to_df(records, ["TSD", "ND"])
        if df.empty:
            return df
        # Prefer TSD (includes pumped storage pumping) over ND.
        col = "TSD" if "TSD" in df.columns else "ND"
        df = df[[col]].rename(columns={col: COLUMN_LOAD})
        return normalise_index(df, STANDARD_TZ)

    def get_generation(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        """Return embedded wind and solar generation from the demand dataset.

        For full BM-connected generation mix (CCGT, nuclear, etc.) use the
        ``"elexon"`` provider.
        """
        records = self._fetch_records(start, end)
        cols = ["EMBEDDED_WIND_GENERATION", "EMBEDDED_SOLAR_GENERATION"]
        df = self._records_to_df(records, cols)
        if df.empty:
            return df
        df = df.rename(columns={
            "EMBEDDED_WIND_GENERATION": "wind_embedded_mw",
            "EMBEDDED_SOLAR_GENERATION": "solar_embedded_mw",
        })
        return normalise_index(df, STANDARD_TZ)

    def get_co2_intensity(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        frame = _carbon_intensity_frame(
            _carbon_records("intensity", start, end), "actual"
        ).rename(columns={"actual": "co2_consumption_g_kwh"})
        frame.attrs.update({
            "source_url": _CARBON_API,
            "license": _CARBON_LICENSE,
            "unit": "gCO2/kWh",
        })
        return frame

    def get_co2_forecast(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        frame = _carbon_intensity_frame(
            _carbon_records("intensity", start, end), "forecast"
        ).rename(columns={"forecast": "co2_forecast_g_kwh"})
        frame.attrs.update({
            "source_url": _CARBON_API,
            "license": _CARBON_LICENSE,
            "unit": "gCO2/kWh",
        })
        return frame

    def get_generation_share(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        frame = _generation_share_frame(_carbon_records("generation", start, end))
        frame.attrs.update({
            "source_url": _CARBON_API,
            "license": _CARBON_LICENSE,
            "unit": "percent",
        })
        return frame

    def zones(self) -> set[str]:
        return {"GB"}

    def capabilities(self) -> set[str]:
        return {
            "load", "generation", "co2_intensity", "co2_forecast",
            "generation_share",
        }

    def name(self) -> str:
        return "NESO Data Portal (Great Britain)"


def register() -> None:
    """Register the NESO provider. Called automatically on import."""
    register_provider("neso", NesoProvider())


register()
