"""Swiss national load forecasts from the SFOE Energy Dashboard API."""

from __future__ import annotations

from typing import Any

import pandas as pd

from clarigrid.core.exceptions import ProviderError
from clarigrid.core.http import get_json
from clarigrid.core.interface import DataProvider
from clarigrid.core.registry import register_provider
from clarigrid.utils.time import normalise_index

_BASE = "https://energiedashboard.ch/api/v1/datasets"
_DATASET_ID = "stromverbrauch-prognose-sdsc"
_METADATA_URL = f"{_BASE}/{_DATASET_ID}"
_DATA_URL = f"{_METADATA_URL}/data"
_SOURCE_URL = (
    "https://opendata.swiss/en/dataset/"
    "energiedashboard-ch-stromverbrauch-prognose-sdsc"
)
_DOCUMENTATION_URL = "https://www.bfe-ogd.ch/apiguide_edb_de_en.pdf"
_LICENSE_URL = "https://opendata.swiss/en/terms-of-use#terms_by"
_LOCAL_TZ = "Europe/Zurich"
_PAGE_SIZE = 1000
_EXPECTED_SCHEMA = {
    "datum": "date",
    "endverbrauch_prognose_min_gwh": "bigint",
    "endverbrauch_prognose_mittel_gwh": "bigint",
    "endverbrauch_prognose_max_gwh": "bigint",
}
_VALUE_COLUMNS = {
    "endverbrauch_prognose_min_gwh": "load_forecast_min_mw",
    "endverbrauch_prognose_mittel_gwh": "load_forecast_mw",
    "endverbrauch_prognose_max_gwh": "load_forecast_max_mw",
}


def _date(value: str | pd.Timestamp) -> str:
    """Return the Swiss calendar date accepted by the daily API."""
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert(_LOCAL_TZ).tz_localize(None)
    return timestamp.date().isoformat()


def _validate_metadata(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise ProviderError("SFOE returned invalid dataset metadata.")
    if payload.get("id") != _DATASET_ID:
        raise ProviderError("SFOE returned metadata for an unexpected dataset.")
    if payload.get("tableName") != "ogd110_strom_verbrauch_prognose":
        raise ProviderError("SFOE changed the load-forecast dataset table.")

    schema = payload.get("schema")
    if not isinstance(schema, list) or any(not isinstance(item, dict) for item in schema):
        raise ProviderError("SFOE returned invalid load-forecast schema metadata.")
    observed = {item.get("columnName"): item.get("dataType") for item in schema}
    if any(observed.get(name) != kind for name, kind in _EXPECTED_SCHEMA.items()):
        raise ProviderError("SFOE changed the load-forecast dataset schema.")

    updated_at = payload.get("updatedAt")
    if not isinstance(updated_at, str):
        raise ProviderError("SFOE metadata is missing its update timestamp.")
    return updated_at


def _fetch_rows(
    start: str | pd.Timestamp, end: str | pd.Timestamp
) -> tuple[list[dict[str, Any]], str]:
    updated_at = _validate_metadata(get_json(_METADATA_URL))
    params: dict[str, str | int] = {
        "from": _date(start),
        "to": _date(end),
        "limit": _PAGE_SIZE,
        "offset": 0,
    }
    rows: list[dict[str, Any]] = []
    while True:
        payload = get_json(_DATA_URL, params)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ProviderError("SFOE returned an invalid load-forecast response.")
        page = payload["data"]
        if any(not isinstance(row, dict) for row in page):
            raise ProviderError("SFOE returned an invalid load-forecast record.")
        response_meta = payload.get("meta")
        if not isinstance(response_meta, dict) or response_meta.get("count") != len(page):
            raise ProviderError("SFOE returned inconsistent pagination metadata.")
        rows.extend(page)
        if len(page) < _PAGE_SIZE:
            break
        params["offset"] = int(params["offset"]) + _PAGE_SIZE
    return rows, updated_at


def _parse_rows(rows: list[dict[str, Any]], updated_at: str) -> pd.DataFrame:
    if not rows:
        frame = pd.DataFrame(
            {column: pd.Series(dtype=float) for column in _VALUE_COLUMNS.values()},
            index=pd.DatetimeIndex([], tz="UTC", name="utc_time"),
        )
    else:
        required = set(_EXPECTED_SCHEMA)
        if any(required.difference(row) for row in rows):
            raise ProviderError("SFOE load forecast is missing required fields.")

        dates = pd.to_datetime([row["datum"] for row in rows], errors="coerce")
        if dates.hasnans:
            raise ProviderError("SFOE returned an invalid forecast date.")
        if dates.duplicated().any():
            raise ProviderError("SFOE returned duplicate load-forecast dates.")
        local_starts = dates.tz_localize(_LOCAL_TZ)
        local_ends = local_starts + pd.DateOffset(days=1)
        period_hours = (
            local_ends.tz_convert("UTC") - local_starts.tz_convert("UTC")
        ) / pd.Timedelta(hours=1)

        values: dict[str, Any] = {}
        for source, target in _VALUE_COLUMNS.items():
            raw = [row[source] for row in rows]
            numeric = pd.to_numeric(pd.Series(raw), errors="coerce")
            if any(value is not None and pd.isna(parsed) for value, parsed in zip(raw, numeric)):
                raise ProviderError(f"SFOE returned a non-numeric value for {source!r}.")
            values[target] = numeric.to_numpy() * 1000.0 / period_hours.to_numpy()
        frame = pd.DataFrame(values, index=local_starts.tz_convert("UTC"))
        if (
            (frame["load_forecast_min_mw"] > frame["load_forecast_mw"])
            | (frame["load_forecast_mw"] > frame["load_forecast_max_mw"])
        ).any():
            raise ProviderError("SFOE returned invalid load-forecast confidence bounds.")
        frame = normalise_index(frame.sort_index())

    frame.attrs.update({
        "source_url": _SOURCE_URL,
        "documentation_url": _DOCUMENTATION_URL,
        "license": "Open use; source attribution required",
        "license_url": _LICENSE_URL,
        "attribution": (
            "Swiss Federal Office of Energy (SFOE), SDSC — "
            "energiedashboard.ch Power consumption forecast SDSC"
        ),
        "data_updated_at": updated_at,
        "unit": "MW (daily average)",
        "source_unit": "GWh/day",
        "transformation": (
            "daily GWh multiplied by 1000 and divided by the actual "
            "Europe/Zurich calendar-day hours"
        ),
        "resolution": "daily",
        "api_version": "1.0.0",
        "authentication": "none",
        "page_size": _PAGE_SIZE,
        "query_limit": _PAGE_SIZE,
        "rate_limit_requests_per_second": None,
    })
    return frame


class SFOEProvider(DataProvider):
    """Daily Swiss electricity-consumption forecasts from SFOE and SDSC."""

    def get_prices(
        self, zone: str, start: str | pd.Timestamp, end: str | pd.Timestamp, **kwargs: Any
    ) -> pd.DataFrame:
        raise NotImplementedError("SFOE price support is not implemented.")

    def get_load(
        self, zone: str, start: str | pd.Timestamp, end: str | pd.Timestamp, **kwargs: Any
    ) -> pd.DataFrame:
        raise NotImplementedError("SFOE actual-load support is not implemented.")

    def get_generation(
        self, zone: str, start: str | pd.Timestamp, end: str | pd.Timestamp, **kwargs: Any
    ) -> pd.DataFrame:
        raise NotImplementedError("SFOE generation support is not implemented.")

    def get_load_forecast(
        self, zone: str, start: str | pd.Timestamp, end: str | pd.Timestamp, **kwargs: Any
    ) -> pd.DataFrame:
        if zone.upper() != "CH":
            raise ValueError(f"SFOE only covers zone 'CH'; got {zone!r}.")
        rows, updated_at = _fetch_rows(start, end)
        return _parse_rows(rows, updated_at)

    def capabilities(self) -> set[str]:
        return {"load_forecast"}

    def zones(self) -> set[str]:
        return {"CH"}

    def name(self) -> str:
        return "Swiss Federal Office of Energy (SFOE)"


register_provider("sfoe", SFOEProvider())
