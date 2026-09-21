"""Elhub open energy-data provider for Norwegian price areas."""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pandas as pd

from clarigrid.core.exceptions import ProviderError
from clarigrid.core.http import get_json
from clarigrid.core.interface import DataProvider
from clarigrid.core.registry import register_provider
from clarigrid.utils.time import normalise_index, parse_dt

_BASE = "https://api.elhub.no/energy-data/v0"
_DOCS = "https://api.elhub.no/api/energy-data"
_LICENSE = "CC BY 4.0"
_LOCAL_TZ = "Europe/Oslo"
_MIN_REQUEST_INTERVAL = 0.2
_ZONES = {"NO1", "NO2", "NO3", "NO4", "NO5"}
_DATASETS = {
    "load": ("CONSUMPTION_PER_GROUP_MBA_HOUR", "consumptionPerGroupMbaHour"),
    "generation": ("PRODUCTION_PER_GROUP_MBA_HOUR", "productionPerGroupMbaHour"),
}
_GENERATION_COLUMNS = {
    "hydro": "hydro_mw",
    "other": "other_mw",
    "solar": "solar_mw",
    "thermal": "thermal_mw",
    "wind": "wind_mw",
}


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name="utc_time"))


def _query_time(value: str | pd.Timestamp) -> pd.Timestamp:
    return pd.Timestamp(parse_dt(value, _LOCAL_TZ))


def _chunks(
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
) -> Iterator[tuple[pd.Timestamp, pd.Timestamp]]:
    """Yield Elhub's maximum one-calendar-month query windows."""
    cursor = _query_time(start)
    final = _query_time(end)
    while cursor <= final:
        chunk_end = min(cursor + pd.DateOffset(months=1), final)
        yield cursor, chunk_end
        if chunk_end == final:
            break
        cursor = chunk_end


def _rows(payload: dict[str, Any], attribute: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    data = payload.get("data", [])
    if data is None:
        data = []
    if not isinstance(data, list):
        raise ProviderError("Elhub returned an invalid data collection.")
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("attributes"), dict):
            raise ProviderError("Elhub returned an invalid data resource.")
        values = item["attributes"].get(attribute, [])
        if values is None:
            values = []
        if not isinstance(values, list) or any(not isinstance(row, dict) for row in values):
            raise ProviderError("Elhub returned an invalid interval collection.")
        rows.extend(values)
    return rows


def _interval_mw(row: dict[str, Any]) -> tuple[pd.Timestamp, float]:
    start_value = row.get("startTime")
    end_value = row.get("endTime")
    quantity_value = row.get("quantityKwh")
    if (
        not isinstance(start_value, str)
        or not isinstance(end_value, str)
        or not isinstance(quantity_value, (int, float))
        or isinstance(quantity_value, bool)
    ):
        raise ProviderError("Elhub returned an invalid interval or quantity.")
    try:
        start = pd.Timestamp(start_value)
        end = pd.Timestamp(end_value)
    except ValueError as exc:
        raise ProviderError("Elhub returned an invalid interval or quantity.") from exc
    if start.tzinfo is None or end.tzinfo is None:
        raise ProviderError("Elhub returned an interval without a UTC offset.")
    start = start.tz_convert("UTC")
    end = end.tz_convert("UTC")
    hours = (end - start).total_seconds() / 3600
    if hours <= 0:
        raise ProviderError("Elhub returned a non-positive interval.")
    return start, float(quantity_value) / 1000 / hours


def _parse_response(payload: dict[str, Any], dataset: str) -> pd.DataFrame:
    """Convert Elhub interval energy in kWh to average power in MW."""
    if dataset not in _DATASETS:
        raise ValueError(f"Unsupported Elhub dataset: {dataset!r}")
    rows = _rows(payload, _DATASETS[dataset][1])
    if not rows:
        frame = _empty_frame()
    elif dataset == "load":
        load_values: dict[pd.Timestamp, float] = {}
        for row in rows:
            timestamp, value = _interval_mw(row)
            load_values[timestamp] = load_values.get(timestamp, 0.0) + value
        frame = pd.DataFrame.from_dict(load_values, orient="index", columns=["load_mw"])
    else:
        generation_values: list[dict[str, Any]] = []
        for row in rows:
            group = row.get("productionGroup")
            if not isinstance(group, str) or group not in _GENERATION_COLUMNS:
                raise ProviderError(f"Elhub returned an unknown production group: {group!r}")
            timestamp, value = _interval_mw(row)
            generation_values.append({
                "utc_time": timestamp,
                "column": _GENERATION_COLUMNS[group],
                "mw": value,
            })
        raw = pd.DataFrame(generation_values)
        frame = raw.pivot_table(index="utc_time", columns="column", values="mw", aggfunc="sum")
        frame.columns.name = None
    frame = normalise_index(frame.sort_index())
    frame.attrs.update({
        "source_url": f"{_BASE}/price-areas",
        "documentation_url": _DOCS,
        "license": _LICENSE,
        "unit": "MW",
        "source_unit": "kWh per interval",
        "resolution": "hourly",
        "api_version": "v0",
        "authentication": "none",
        "max_query_window": "one month",
        "rate_limit": "5 requests/second/IP",
    })
    updated = (payload.get("meta") or {}).get("lastUpdated")
    if updated:
        frame.attrs["data_updated_at"] = updated
    return frame


def _fetch(
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    dataset: str,
) -> pd.DataFrame:
    if zone not in _ZONES:
        raise ValueError(f"Elhub zone must be one of {sorted(_ZONES)}; got {zone!r}.")
    name, _attribute = _DATASETS[dataset]
    frames = []
    last_request = 0.0
    for chunk_start, chunk_end in _chunks(start, end):
        delay = _MIN_REQUEST_INTERVAL - (time.monotonic() - last_request)
        if delay > 0:
            time.sleep(delay)
        payload = get_json(
            f"{_BASE}/price-areas/{zone}",
            {
                "dataset": name,
                "startDate": chunk_start.isoformat(),
                "endDate": chunk_end.isoformat(),
            },
        )
        if not isinstance(payload, dict):
            raise ProviderError("Elhub returned a non-object response.")
        last_request = time.monotonic()
        frames.append(_parse_response(payload, dataset))
    populated = [frame for frame in frames if not frame.empty]
    if not populated:
        return frames[0] if frames else _empty_frame()
    result = normalise_index(pd.concat(populated).sort_index())
    result = pd.DataFrame(result.groupby(level=0).sum(min_count=1))
    result = normalise_index(result)
    result.attrs.update(populated[-1].attrs)
    return result


class ElhubProvider(DataProvider):
    """Hourly Norwegian consumption and production from Elhub."""

    def get_prices(
        self,
        zone: str,
        start: str | pd.Timestamp,
        end: str | pd.Timestamp,
        **kwargs: Any,
    ) -> pd.DataFrame:
        raise NotImplementedError("Elhub's Energy Data API does not publish market prices.")

    def get_load(
        self,
        zone: str,
        start: str | pd.Timestamp,
        end: str | pd.Timestamp,
        **kwargs: Any,
    ) -> pd.DataFrame:
        return _fetch(zone, start, end, "load")

    def get_generation(
        self,
        zone: str,
        start: str | pd.Timestamp,
        end: str | pd.Timestamp,
        **kwargs: Any,
    ) -> pd.DataFrame:
        return _fetch(zone, start, end, "generation")

    def zones(self) -> set[str]:
        return set(_ZONES)

    def capabilities(self) -> set[str]:
        return {"load", "generation"}

    def name(self) -> str:
        return "Elhub Energy Data"


def register() -> None:
    register_provider("elhub", ElhubProvider())


register()
