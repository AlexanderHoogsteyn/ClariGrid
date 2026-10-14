"""Gas Networks Ireland daily Republic of Ireland gas demand."""

from __future__ import annotations

from typing import Any

import pandas as pd

from clarigrid.core.exceptions import ProviderError
from clarigrid.core.http import get_json
from clarigrid.core.interface import DataProvider
from clarigrid.core.registry import register_provider
from clarigrid.utils.time import normalise_index

_PACKAGE_API = "https://data.gov.ie/api/3/action/package_show"
_PACKAGE_ID = "dailygasdemandireland"
_SOURCE_URL = "https://data.gov.ie/dataset/dailygasdemandireland"
_LICENSE_URL = "https://data.gov.ie/psi"
_LOCAL_TZ = "Europe/Dublin"
_FIELDS = {
    "Daily Metered ": "daily_metered_gwh",
    "Non Daily Metered (NDM)": "non_daily_metered_gwh",
    "Large Daily Metered (LDM) non Power Gen": "large_daily_metered_non_power_gwh",
    "Power Generation": "power_generation_gas_demand_gwh",
    "Total ROI demand": "gas_demand_gwh",
}


def _calendar_day(value: str | pd.Timestamp) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert(_LOCAL_TZ).tz_localize(None)
    return pd.Timestamp(timestamp.normalize())


def _resource(payload: Any) -> tuple[str, str | None]:
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ProviderError("data.gov.ie returned invalid GNI dataset metadata.")
    result = payload.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("resources"), list):
        raise ProviderError("data.gov.ie returned invalid GNI dataset metadata.")
    resources = [
        item
        for item in result["resources"]
        if isinstance(item, dict)
        and str(item.get("format", "")).upper() == "JSON"
        and item.get("state") == "active"
        and isinstance(item.get("url"), str)
    ]
    if len(resources) != 1 or not resources[0]["url"].startswith("https://www.gasnetworks.ie/"):
        raise ProviderError("data.gov.ie did not publish one active GNI JSON resource.")
    return resources[0]["url"], result.get("metadata_modified")


def _parse_records(
    records: Any,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
) -> pd.DataFrame:
    if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
        raise ProviderError("Gas Networks Ireland returned an invalid record collection.")
    required = {"Date", *_FIELDS}
    if any(required.difference(row) for row in records):
        raise ProviderError("Gas Networks Ireland response is missing required fields.")

    timestamps = pd.to_datetime(
        [row["Date"] for row in records], format="%Y-%m-%d", utc=True, errors="coerce"
    )
    if timestamps.hasnans or timestamps.duplicated().any():
        raise ProviderError("Gas Networks Ireland returned invalid or duplicate dates.")

    data: dict[str, Any] = {}
    for source, target in _FIELDS.items():
        values = pd.to_numeric([row[source] for row in records], errors="coerce")
        if pd.isna(values).any():
            raise ProviderError(f"Gas Networks Ireland returned invalid values for {source!r}.")
        data[target] = values

    frame = pd.DataFrame(data, index=timestamps)
    start_day = pd.Timestamp(_calendar_day(start), tz="UTC")
    end_day = pd.Timestamp(_calendar_day(end), tz="UTC")
    frame = normalise_index(frame.loc[start_day:end_day])
    frame.attrs.update({
        "source_url": _SOURCE_URL,
        "documentation_url": _SOURCE_URL,
        "license": "Irish Public Sector Information Licence (CC BY 4.0)",
        "license_url": _LICENSE_URL,
        "attribution": (
            "Contains Irish Public Sector Data licensed under a Creative Commons "
            "Attribution 4.0 International (CC BY 4.0) licence."
        ),
        "unit": "GWh",
        "resolution": "daily",
        "update_frequency": "quarterly",
        "authentication": "none",
        "api_version": "CKAN Action API v3",
        "pagination": None,
        "query_limit": None,
        "rate_limit": None,
    })
    if len(timestamps):
        frame.attrs["coverage_start"] = timestamps.min().date().isoformat()
        frame.attrs["coverage_end"] = timestamps.max().date().isoformat()
    return frame


def _fetch(start: str | pd.Timestamp, end: str | pd.Timestamp) -> pd.DataFrame:
    metadata = get_json(_PACKAGE_API, {"id": _PACKAGE_ID})
    resource_url, updated_at = _resource(metadata)
    frame = _parse_records(get_json(resource_url), start, end)
    if isinstance(updated_at, str):
        frame.attrs["data_updated_at"] = updated_at
    return frame


class GNIProvider(DataProvider):
    """Daily sector and total gas demand published by Gas Networks Ireland."""

    def get_gas_demand(
        self,
        zone: str,
        start: str | pd.Timestamp,
        end: str | pd.Timestamp,
        **kwargs: Any,
    ) -> pd.DataFrame:
        if zone.upper() != "IE":
            raise ValueError(f"Gas Networks Ireland only covers zone 'IE'; got {zone!r}.")
        return _fetch(start, end)

    def get_prices(
        self, zone: str, start: str | pd.Timestamp, end: str | pd.Timestamp, **kwargs: Any
    ) -> pd.DataFrame:
        raise NotImplementedError("Gas Networks Ireland does not publish electricity prices.")

    def get_load(
        self, zone: str, start: str | pd.Timestamp, end: str | pd.Timestamp, **kwargs: Any
    ) -> pd.DataFrame:
        raise NotImplementedError("Gas Networks Ireland publishes gas, not electricity, demand.")

    def get_generation(
        self, zone: str, start: str | pd.Timestamp, end: str | pd.Timestamp, **kwargs: Any
    ) -> pd.DataFrame:
        raise NotImplementedError("Gas Networks Ireland does not publish electricity generation.")

    def capabilities(self) -> set[str]:
        return {"gas_demand"}

    def zones(self) -> set[str]:
        return {"IE"}

    def name(self) -> str:
        return "Gas Networks Ireland"


register_provider("gni", GNIProvider())
