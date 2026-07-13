"""RTE Eco2mix provider for France.

The official ODRÉ datasets are open and require no API key. Consolidated /
definitive history and recent real-time data share one schema, so this
provider stitches both sources and prefers real-time records on overlap.
"""

from __future__ import annotations

import time
from typing import Any

import pandas as pd

from clarigrid.core.http import get_json
from clarigrid.core.interface import DataProvider
from clarigrid.core.registry import register_provider
from clarigrid.utils.time import normalise_index, parse_dt

_HISTORICAL_URL = (
    "https://odre.opendatasoft.com/api/explore/v2.1/catalog/datasets/"
    "eco2mix-national-cons-def/records"
)
_REALTIME_URL = (
    "https://reseaux-energies-rte.opendatasoft.com/api/explore/v2.1/catalog/"
    "datasets/eco2mix-national-tr/records"
)
_SOURCE_URL = "https://www.rte-france.com/en/data-publications/eco2mix"
_LICENSE = "Licence Ouverte / Open Licence 2.0"
_PAGE_SIZE = 100
_RAW_CACHE_TTL_SECONDS = 60.0
_RAW_CACHE: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}

_GENERATION_COLUMNS = {
    "fioul": "oil_mw",
    "charbon": "hard_coal_mw",
    "gaz": "gas_mw",
    "nucleaire": "nuclear_mw",
    "eolien_terrestre": "wind_onshore_mw",
    "eolien_offshore": "wind_offshore_mw",
    "solaire": "solar_mw",
    "hydraulique": "hydro_mw",
    "bioenergies_dechets": "waste_mw",
    "bioenergies_biomasse": "biomass_mw",
    "bioenergies_biogaz": "biogas_mw",
    "destockage_batterie": "battery_discharge_mw",
}

_SCHEDULE_COLUMNS = {
    "ech_comm_angleterre": "schedule_gb_mw",
    "ech_comm_espagne": "schedule_es_mw",
    "ech_comm_italie": "schedule_it_mw",
    "ech_comm_suisse": "schedule_ch_mw",
    "ech_comm_allemagne_belgique": "schedule_de_be_mw",
}


def _iso_utc(value: str | pd.Timestamp) -> str:
    return parse_dt(value).isoformat().replace("+00:00", "Z")


def _fetch_dataset(url: str, start: str, end: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    offset = 0
    where = f"date_heure >= '{_iso_utc(start)}' AND date_heure <= '{_iso_utc(end)}'"
    while True:
        payload = get_json(url, {
            "where": where,
            "limit": _PAGE_SIZE,
            "offset": offset,
            "order_by": "date_heure asc",
            "timezone": "UTC",
        })
        page = payload.get("results") or []
        records.extend(page)
        offset += _PAGE_SIZE
        if offset >= int(payload.get("total_count", 0)):
            break
    return records


def _fetch_records(
    start: str | pd.Timestamp, end: str | pd.Timestamp
) -> list[dict[str, Any]]:
    """Fetch both lifecycle datasets and deduplicate by timestamp."""
    key = (str(start), str(end))
    cached = _RAW_CACHE.get(key)
    if cached and time.monotonic() - cached[0] < _RAW_CACHE_TTL_SECONDS:
        return [dict(record) for record in cached[1]]

    ranked: list[dict[str, Any]] = []
    for rank, url in enumerate((_HISTORICAL_URL, _REALTIME_URL)):
        for record in _fetch_dataset(url, str(start), str(end)):
            ranked.append({**record, "_source_rank": rank})

    if not ranked:
        records: list[dict[str, Any]] = []
    else:
        raw = pd.DataFrame.from_records(ranked)
        raw["date_heure"] = pd.to_datetime(raw["date_heure"], utc=True, errors="coerce")
        raw = raw.dropna(subset=["date_heure"]).sort_values(
            ["date_heure", "_source_rank"]
        )
        raw = raw.drop_duplicates("date_heure", keep="last").drop(columns="_source_rank")
        records = raw.to_dict("records")

    _RAW_CACHE[key] = (time.monotonic(), records)
    return [dict(record) for record in records]


def _records_frame(
    records: list[dict[str, Any]], columns: dict[str, str]
) -> pd.DataFrame:
    if not records:
        return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name="utc_time"))
    raw = pd.DataFrame.from_records(records)
    raw["date_heure"] = pd.to_datetime(raw["date_heure"], utc=True, errors="coerce")
    raw = raw.dropna(subset=["date_heure"]).set_index("date_heure")
    available = {source: target for source, target in columns.items() if source in raw}
    frame = raw[list(available)].rename(columns=available)
    for column in frame:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.groupby(level=0).first().sort_index().dropna(how="all")
    return normalise_index(frame)


def _generation_frame(records: list[dict[str, Any]]) -> pd.DataFrame:
    frame = _records_frame(records, _GENERATION_COLUMNS)
    if not records:
        return frame
    # Older records only expose aggregate wind. Use it as onshore when the
    # detailed split is unavailable, while avoiding double counting offshore.
    raw = _records_frame(records, {"eolien": "wind_total_mw"})
    if "wind_total_mw" in raw:
        frame = frame.reindex(frame.index.union(raw.index))
        if "wind_onshore_mw" not in frame:
            frame["wind_onshore_mw"] = raw["wind_total_mw"]
        else:
            detailed = frame["wind_onshore_mw"]
            frame["wind_onshore_mw"] = detailed.fillna(raw["wind_total_mw"])
    return normalise_index(frame.sort_index())


def _metadata(
    frame: pd.DataFrame,
    endpoint: str,
    *,
    unit: str,
    **metadata: Any,
) -> pd.DataFrame:
    frame.attrs.update({
        "source_url": _SOURCE_URL,
        "api_url": endpoint,
        "historical_api_url": _HISTORICAL_URL,
        "realtime_api_url": _REALTIME_URL,
        "license": _LICENSE,
        "unit": unit,
        "area": "FR bidding zone",
        "data_lifecycle": "real-time, consolidated or definitive",
        **metadata,
    })
    return frame


class RTEProvider(DataProvider):
    """RTE Eco2mix national electricity data."""

    def get_prices(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        raise NotImplementedError(
            "Eco2mix does not publish a canonical day-ahead price series. "
            "Connect 'energycharts' or 'entsoe' for FR prices."
        )

    def _records(self, start: str, end: str) -> list[dict[str, Any]]:
        return _fetch_records(start, end)

    def get_load(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        frame = _records_frame(self._records(start, end), {"consommation": "load_mw"})
        return _metadata(frame, _REALTIME_URL, unit="MW")

    def get_load_forecast(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        frame = _records_frame(self._records(start, end), {
            "prevision_j": "load_forecast_mw",
            "prevision_j1": "load_day_ahead_forecast_mw",
        })
        return _metadata(frame, _REALTIME_URL, unit="MW")

    def get_generation(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        frame = _generation_frame(self._records(start, end))
        return _metadata(frame, _REALTIME_URL, unit="MW")

    def get_generation_share(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        generation = self.get_generation(zone, start, end)
        if generation.empty:
            return generation
        positive = generation.clip(lower=0)
        total = positive.sum(axis=1).mask(lambda values: values.eq(0))
        frame = positive.div(total, axis=0).mul(100).rename(columns={
            column: column.removesuffix("_mw") + "_share_pct"
            for column in positive.columns
        })
        return _metadata(frame, _REALTIME_URL, unit="percent")

    def get_renewable_share(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        generation = self.get_generation(zone, start, end)
        if generation.empty:
            return generation
        renewable = [
            column for column in generation
            if column in {
                "wind_onshore_mw", "wind_offshore_mw", "solar_mw", "hydro_mw",
                "waste_mw", "biomass_mw", "biogas_mw",
            }
        ]
        positive = generation.clip(lower=0)
        total = positive.sum(axis=1).mask(lambda values: values.eq(0))
        frame = positive[renewable].sum(axis=1).div(total).mul(100).to_frame(
            "renewable_share_generation_pct"
        )
        return _metadata(frame, _REALTIME_URL, unit="percent")

    def get_physical_flows(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        frame = _records_frame(
            self._records(start, end), {"ech_physiques": "flow_net_mw"}
        )
        return _metadata(
            frame, _REALTIME_URL, unit="MW",
            sign_convention="positive=import, negative=export",
        )

    def get_commercial_schedule(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        frame = _records_frame(self._records(start, end), _SCHEDULE_COLUMNS)
        return _metadata(
            frame, _REALTIME_URL, unit="MW",
            sign_convention="positive=import, negative=export",
        )

    def get_co2_intensity(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        frame = _records_frame(
            self._records(start, end), {"taux_co2": "co2_production_g_kwh"}
        )
        return _metadata(frame, _REALTIME_URL, unit="gCO2eq/kWh")

    def zones(self) -> set[str]:
        return {"FR"}

    def capabilities(self) -> set[str]:
        return {
            "load", "load_forecast", "generation", "generation_share",
            "renewable_share", "physical_flows", "commercial_schedule",
            "co2_intensity",
        }

    def name(self) -> str:
        return "RTE Eco2mix (France)"


def register() -> None:
    register_provider("rte", RTEProvider())


register()
