"""Energinet Energi Data Service provider for Denmark.

The catalogue API is public and requires no key.  This provider normalises
both Danish bidding zones (DK1 and DK2), including the October 2025 transition
from hourly ``Elspotprices`` to 15-minute ``DayAheadPrices``.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pandas as pd

from clarigrid.core.http import get_json
from clarigrid.core.interface import DataProvider
from clarigrid.core.registry import register_provider
from clarigrid.utils.time import normalise_index

_BASE = "https://api.energidataservice.dk"
_SOURCE_URL = "https://www.energidataservice.dk/"
_LICENSE = "CC BY 4.0"
_PRICE_CUTOVER = pd.Timestamp("2025-10-01")
_PAGE_SIZE = 10_000
_RAW_CACHE_TTL_SECONDS = 60.0
_RAW_CACHE: dict[tuple, tuple[float, list[dict[str, Any]]]] = {}

_GENERATION_COLUMNS = {
    "OffshoreWindPower": "wind_offshore_mw",
    "OnshoreWindPower": "wind_onshore_mw",
    "HydroPower": "hydro_mw",
    "SolarPower": "solar_mw",
    "SolarPowerSelfCon": "solar_self_consumption_mw",
    "Biomass": "biomass_mw",
    "Biogas": "biogas_mw",
    "Waste": "waste_mw",
    "FossilGas": "gas_mw",
    "FossilOil": "oil_mw",
    "FossilHardCoal": "hard_coal_mw",
}

_FORECAST_TYPES = {
    "Offshore Wind": "wind_offshore_forecast_mw",
    "Onshore Wind": "wind_onshore_forecast_mw",
    "Solar": "solar_forecast_mw",
}

_EXCHANGE_BORDERS = {
    "SE": "sweden",
    "GE": "germany",
    "NL": "netherlands",
    "NO": "norway",
    "GB": "great_britain",
}


def _fetch_records(
    dataset: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    filters: dict[str, list[str]] | None = None,
    sort: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch all records for a bounded dataset query."""
    filter_key = json.dumps(filters or {}, sort_keys=True, separators=(",", ":"))
    cache_key = (dataset, str(start), str(end), filter_key, sort)
    cached = _RAW_CACHE.get(cache_key)
    if cached and time.monotonic() - cached[0] < _RAW_CACHE_TTL_SECONDS:
        return [dict(record) for record in cached[1]]

    records: list[dict[str, Any]] = []
    offset = 0
    while True:
        params: dict[str, Any] = {
            "start": str(start),
            "end": str(end),
            "offset": offset,
            "limit": _PAGE_SIZE,
            "timezone": "UTC",
        }
        if filters:
            params["filter"] = json.dumps(filters, separators=(",", ":"))
        if sort:
            params["sort"] = sort
        payload = get_json(f"{_BASE}/dataset/{dataset}", params)
        page = payload.get("records", [])
        records.extend(page)
        if len(page) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    _RAW_CACHE[cache_key] = (time.monotonic(), records)
    return [dict(record) for record in records]


def _records_frame(
    records: list[dict[str, Any]],
    time_field: str,
    columns: dict[str, str],
) -> pd.DataFrame:
    """Convert selected record fields into a canonical UTC DataFrame."""
    if not records:
        return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name="utc_time"))
    raw = pd.DataFrame(records)
    if time_field not in raw:
        return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name="utc_time"))
    raw[time_field] = pd.to_datetime(raw[time_field], utc=True, errors="coerce")
    raw = raw.dropna(subset=[time_field]).set_index(time_field)
    available = {source: target for source, target in columns.items() if source in raw}
    frame = raw[list(available)].rename(columns=available)
    for column in frame:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.groupby(level=0).sum(min_count=1).sort_index()
    return normalise_index(frame)


def _with_metadata(
    frame: pd.DataFrame,
    dataset: str,
    *,
    unit: str,
    **metadata: Any,
) -> pd.DataFrame:
    frame.attrs.update({
        "source_url": f"{_SOURCE_URL}tso-electricity/{dataset.lower()}",
        "license": _LICENSE,
        "unit": unit,
        **metadata,
    })
    return frame


def _interval_energy_to_power(frame: pd.DataFrame) -> pd.DataFrame:
    """Convert interval energy in MWh to average MW from index spacing."""
    if frame.empty or len(frame.index) < 2:
        return frame
    interval_hours = frame.index.to_series().diff().dropna().median().total_seconds() / 3600
    if interval_hours <= 0:
        raise ValueError("Cannot infer a positive interval for MWh-to-MW conversion.")
    return frame / interval_hours


class EnerginetProvider(DataProvider):
    """Energinet's open data API for DK1 and DK2."""

    def get_prices(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
        frames: list[pd.DataFrame] = []
        filters = {"PriceArea": [zone]}

        if start_ts < _PRICE_CUTOVER:
            legacy_end = min(end_ts, _PRICE_CUTOVER)
            records = _fetch_records(
                "Elspotprices", start, str(legacy_end), filters=filters, sort="HourUTC"
            )
            frames.append(_records_frame(records, "HourUTC", {
                "SpotPriceEUR": "price_eur_mwh",
            }))
        if end_ts >= _PRICE_CUTOVER:
            current_start = max(start_ts, _PRICE_CUTOVER)
            records = _fetch_records(
                "DayAheadPrices", str(current_start), end,
                filters=filters, sort="TimeUTC",
            )
            frames.append(_records_frame(records, "TimeUTC", {
                "DayAheadPriceEUR": "price_eur_mwh",
            }))

        frame = pd.concat(frames).sort_index() if frames else pd.DataFrame()
        if not frame.empty:
            frame = frame[~frame.index.duplicated(keep="last")]
            frame = normalise_index(frame)
        return _with_metadata(frame, "DayAheadPrices", unit="EUR/MWh", currency="EUR")

    def _generation_records(self, zone: str, start: str, end: str) -> list[dict[str, Any]]:
        return _fetch_records(
            "GenerationProdTypeExchange",
            start,
            end,
            filters={"PriceArea": [zone], "Version": ["Final"]},
            sort="TimeUTC",
        )

    def get_load(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        frame = _records_frame(
            self._generation_records(zone, start, end),
            "TimeUTC",
            {"GrossCon": "load_mw"},
        )
        return _with_metadata(frame, "GenerationProdTypeExchange", unit="MW")

    def get_generation(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        frame = _records_frame(
            self._generation_records(zone, start, end),
            "TimeUTC",
            _GENERATION_COLUMNS,
        )
        return _with_metadata(frame, "GenerationProdTypeExchange", unit="MW")

    def get_generation_forecast(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        records = _fetch_records(
            "Forecasts_Hour",
            start,
            end,
            filters={"PriceArea": [zone]},
            sort="HourUTC",
        )
        rows = []
        for record in records:
            column = _FORECAST_TYPES.get(record.get("ForecastType"))
            if not column:
                continue
            rows.append({
                "utc_time": pd.to_datetime(record.get("HourUTC"), utc=True),
                "column": column,
                "value": pd.to_numeric(record.get("ForecastDayAhead"), errors="coerce"),
            })
        if not rows:
            frame = pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name="utc_time"))
        else:
            frame = pd.DataFrame(rows).pivot_table(
                index="utc_time", columns="column", values="value", aggfunc="last"
            )
            frame.columns.name = None
            frame = normalise_index(frame.sort_index())
        return _with_metadata(frame, "Forecasts_Hour", unit="MW", forecast_horizon="day-ahead")

    def get_physical_flows(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        records = _fetch_records(
            "ForeignExchange",
            start,
            end,
            filters={"PriceArea": [zone]},
            sort="HourUTC",
        )
        if not records:
            frame = pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name="utc_time"))
        else:
            raw = pd.DataFrame(records)
            raw["HourUTC"] = pd.to_datetime(raw["HourUTC"], utc=True)
            raw = raw.set_index("HourUTC").sort_index()
            columns = {}
            for code, border in _EXCHANGE_BORDERS.items():
                import_column = f"ExchangeImport{code}_MWh"
                export_column = f"ExchangeExport{code}_MWh"
                if import_column not in raw or export_column not in raw:
                    continue
                imported = pd.to_numeric(raw[import_column], errors="coerce")
                exported = pd.to_numeric(raw[export_column], errors="coerce")
                columns[f"flow_{border}_mw"] = imported.fillna(0) + exported.fillna(0)
            frame = pd.DataFrame(columns, index=raw.index)
            frame = _interval_energy_to_power(normalise_index(frame))
        return _with_metadata(
            frame,
            "ForeignExchange",
            unit="MW",
            sign_convention="positive=import, negative=export",
        )

    def get_co2_intensity(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        records = _fetch_records(
            "CO2Emis", start, end, filters={"PriceArea": [zone]}, sort="Minutes5UTC"
        )
        frame = _records_frame(records, "Minutes5UTC", {
            "CO2Emission": "co2_consumption_g_kwh",
        })
        return _with_metadata(frame, "CO2Emis", unit="gCO2/kWh")

    def get_co2_forecast(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        records = _fetch_records(
            "CO2EmisProg", start, end,
            filters={"PriceArea": [zone]}, sort="Minutes5UTC",
        )
        frame = _records_frame(records, "Minutes5UTC", {
            "CO2Emission": "co2_forecast_g_kwh",
        })
        return _with_metadata(frame, "CO2EmisProg", unit="gCO2/kWh")

    def zones(self) -> set[str]:
        return {"DK1", "DK2"}

    def capabilities(self) -> set[str]:
        return {
            "prices", "load", "generation", "generation_forecast",
            "physical_flows", "co2_intensity", "co2_forecast",
        }

    def name(self) -> str:
        return "Energinet Energi Data Service"


def register() -> None:
    register_provider("energinet", EnerginetProvider())


register()
