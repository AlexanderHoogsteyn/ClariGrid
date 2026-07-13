"""Fingrid Open Data provider for Finland.

The API is free after registration and uses an ``x-api-key`` request header.
Configure it with ``clarigrid keys set fingrid YOUR_KEY`` or the
``CLARIGRID_FINGRID_API_KEY`` environment variable.

Fingrid exposes one numeric time series per dataset ID.  This provider uses
the multi-series endpoint and pivots those rows into Clarigrid's canonical
wide format at the provider boundary.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from typing import Any

import pandas as pd

from clarigrid.core import config
from clarigrid.core.http import get_json
from clarigrid.core.interface import DataProvider
from clarigrid.core.registry import register_provider
from clarigrid.core.types import COLUMN_LOAD
from clarigrid.utils.time import normalise_index, parse_dt

_BASE = "https://data.fingrid.fi/api"
_SOURCE_URL = "https://data.fingrid.fi/en"
_LICENSE = "CC BY 4.0"
_PAGE_SIZE = 20_000
_MIN_REQUEST_INTERVAL = 2.0

# Dataset IDs and source units are documented in Fingrid's public catalogue.
_LOAD = {124: COLUMN_LOAD}  # MWh/h: numerically average MW
_GENERATION = {
    74: "total_generation_mw",  # MWh/h
    75: "wind_onshore_mw",      # MW
}
_GENERATION_FORECAST = {
    241: "total_generation_forecast_mw",
    245: "wind_onshore_forecast_mw",
    248: "solar_forecast_mw",
}
_LOAD_FORECAST = {166: "load_forecast_mw"}
_PHYSICAL_FLOWS = {
    55: "flow_ee_mw",
    57: "flow_no4_mw",
    60: "flow_se1_mw",
    61: "flow_se3_mw",
}
_NTC = {
    112: "ntc_import_ee_mw",  # EE -> FI
    115: "ntc_export_ee_mw",  # FI -> EE
}
_CAPACITY = {
    267: "solar_capacity_mw",
    268: "wind_onshore_capacity_mw",
}
_CO2 = {
    265: "co2_consumption_g_kwh",
    266: "co2_production_g_kwh",
}
_IMBALANCE_PRICES = {319: "imbalance_price_eur_mwh"}
_BALANCING_PRICES = {
    244: "up_regulation_price_eur_mwh",
    106: "down_regulation_price_eur_mwh",
}
_BALANCING_VOLUMES = {
    375: "mfrr_up_mw",
    376: "mfrr_down_mw",
}

_request_lock = threading.Lock()
_last_request_at = 0.0


def _iso_utc(value: str | pd.Timestamp) -> str:
    return parse_dt(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _throttled_get(params: dict[str, Any], api_key: str) -> dict[str, Any]:
    """Call Fingrid while respecting its one-request-per-two-seconds limit."""
    global _last_request_at
    with _request_lock:
        wait = _MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        payload = get_json(
            f"{_BASE}/data",
            params=params,
            headers={"x-api-key": api_key},
        )
        _last_request_at = time.monotonic()
    if not isinstance(payload, dict):
        raise TypeError("Fingrid returned an unexpected non-object response.")
    return payload


def _fetch(
    datasets: Iterable[int],
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    api_key: str,
) -> list[dict]:
    ids = [int(dataset_id) for dataset_id in datasets]
    params: dict[str, Any] = {
        "datasets": ",".join(map(str, ids)),
        "startTime": _iso_utc(start),
        "endTime": _iso_utc(end),
        "page": 1,
        "pageSize": _PAGE_SIZE,
        "sortBy": "startTime",
        "sortOrder": "asc",
    }
    records: list[dict] = []
    while True:
        payload = _throttled_get(params, api_key)
        page = payload.get("data") or []
        if not isinstance(page, list):
            raise TypeError("Fingrid response field 'data' must be a list.")
        records.extend(item for item in page if isinstance(item, dict))

        pagination = payload.get("pagination") or {}
        current = int(pagination.get("currentPage", params["page"]))
        last = int(pagination.get("lastPage", current))
        if current >= last or len(page) < _PAGE_SIZE:
            break
        params["page"] = current + 1
    return records


def _records_to_frame(
    records: list[dict],
    columns: dict[int, str],
    *,
    multipliers: dict[int, float] | None = None,
) -> pd.DataFrame:
    """Pivot Fingrid's long row contract into a canonical wide frame."""
    rows: list[dict[str, Any]] = []
    multipliers = multipliers or {}
    for record in records:
        try:
            dataset_id = int(record["datasetId"])
        except (KeyError, TypeError, ValueError):
            continue
        column = columns.get(dataset_id)
        if column is None:
            continue
        timestamp = pd.to_datetime(record.get("startTime"), utc=True, errors="coerce")
        value = pd.to_numeric(record.get("value"), errors="coerce")
        if pd.isna(timestamp):
            continue
        rows.append({
            "utc_time": timestamp,
            "column": column,
            "value": value * multipliers.get(dataset_id, 1.0),
        })

    if not rows:
        return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name="utc_time"))
    long = pd.DataFrame(rows)
    frame = long.pivot_table(
        index="utc_time", columns="column", values="value", aggfunc="last"
    )
    frame.columns.name = None
    return normalise_index(frame.sort_index())


def _metadata(frame: pd.DataFrame, *, unit: str, **extra: Any) -> pd.DataFrame:
    frame.attrs.update({
        "source_url": _SOURCE_URL,
        "license": _LICENSE,
        "unit": unit,
        **extra,
    })
    return frame


class FingridProvider(DataProvider):
    """Fingrid's Finnish power-system and market time series."""

    def _api_key(self) -> str:
        key = config.get_api_key("fingrid")
        if not key:
            raise RuntimeError(
                "Fingrid API key not configured. Register for free at "
                "https://data.fingrid.fi/en/instructions then run:\n"
                "  clarigrid keys set fingrid YOUR_KEY\n"
                "or set CLARIGRID_FINGRID_API_KEY."
            )
        return key

    def _frame(
        self,
        columns: dict[int, str],
        start: str | pd.Timestamp,
        end: str | pd.Timestamp,
        *,
        multipliers: dict[int, float] | None = None,
        unit: str = "MW",
        **metadata: Any,
    ) -> pd.DataFrame:
        records = _fetch(columns, start, end, self._api_key())
        return _metadata(
            _records_to_frame(records, columns, multipliers=multipliers),
            unit=unit,
            dataset_ids=sorted(columns),
            **metadata,
        )

    def get_prices(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        raise NotImplementedError(
            "Fingrid does not publish day-ahead prices. Connect Energy-Charts "
            "or ENTSO-E for FI prices."
        )

    def get_load(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        return self._frame(_LOAD, start, end)

    def get_generation(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        return self._frame(_GENERATION, start, end)

    def get_load_forecast(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        return self._frame(_LOAD_FORECAST, start, end)

    def get_generation_forecast(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        return self._frame(_GENERATION_FORECAST, start, end)

    def get_physical_flows(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        # Source is export-positive from Finland; Clarigrid is import-positive.
        multipliers = {dataset_id: -1.0 for dataset_id in _PHYSICAL_FLOWS}
        return self._frame(
            _PHYSICAL_FLOWS,
            start,
            end,
            multipliers=multipliers,
            sign_convention="positive=import, negative=export",
        )

    def get_ntc(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        return self._frame(
            _NTC,
            start,
            end,
            direction_convention="direction encoded in column name; values are positive",
        )

    def get_frequency(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        return self._frame({177: "frequency_hz"}, start, end, unit="Hz")

    def get_installed_capacity(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        return self._frame(_CAPACITY, start, end)

    def get_co2_intensity(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        return self._frame(_CO2, start, end, unit="gCO2/kWh")

    def get_imbalance_prices(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        return self._frame(_IMBALANCE_PRICES, start, end, unit="EUR/MWh", currency="EUR")

    def get_balancing_prices(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        return self._frame(_BALANCING_PRICES, start, end, unit="EUR/MWh", currency="EUR")

    def get_balancing_volumes(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        return self._frame(
            _BALANCING_VOLUMES,
            start,
            end,
            direction_convention="up/down direction encoded in column name",
        )

    def capabilities(self) -> set[str]:
        return {
            "load",
            "generation",
            "load_forecast",
            "generation_forecast",
            "physical_flows",
            "ntc",
            "frequency",
            "installed_capacity",
            "co2_intensity",
            "imbalance_prices",
            "balancing_prices",
            "balancing_volumes",
        }

    def zones(self) -> set[str]:
        return {"FI"}

    def name(self) -> str:
        return "Fingrid Open Data (Finland)"


register_provider("fingrid", FingridProvider())
