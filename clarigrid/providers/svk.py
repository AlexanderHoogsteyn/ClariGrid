"""Swedish cross-border physical power flows from Svenska kraftnät."""

from __future__ import annotations

import json
import re
from typing import Any

import pandas as pd

from clarigrid.core.exceptions import ProviderError
from clarigrid.core.http import get_json
from clarigrid.core.interface import DataProvider
from clarigrid.core.registry import register_provider
from clarigrid.utils.time import normalise_index, parse_dt

_API_URL = "https://data.svk.se/api/3/action/datastore_search"
_RESOURCE_ID = "4aa94b88-9f42-4f0a-a6a8-8e47aadc782a"
_SOURCE_URL = "https://data.svk.se/dataset/net_cross_border_physical_flows"
_DOCUMENTATION_URL = "https://data.svk.se/blog/api_guide"
_LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
_PAGE_SIZE = 32_000
_ZONES = {"SE1", "SE2", "SE3", "SE4"}
_REQUIRED_FIELDS = {
    "start_time_utc": "timestamp",
    "from_bidding_zone": "text",
    "to_bidding_zone": "text",
    "power": "numeric",
    "power_unit": "text",
}
_ZONE = re.compile(r"^[A-Z]{2}[1-4]?$")


def _utc_bounds(
    start: str | pd.Timestamp, end: str | pd.Timestamp
) -> tuple[pd.Timestamp, pd.Timestamp]:
    start_utc = parse_dt(start)
    end_utc = parse_dt(end)
    if end_utc == end_utc.normalize():
        end_utc += pd.DateOffset(days=1)
    if end_utc <= start_utc:
        end_utc = start_utc + pd.DateOffset(days=1)
    return start_utc, end_utc


def _fetch_rows(start: str | pd.Timestamp, end: str | pd.Timestamp) -> list[dict[str, Any]]:
    start_utc, end_utc = _utc_bounds(start, end)
    filters = json.dumps(
        {
            "start_time_utc": {
                "gte": start_utc.tz_localize(None).isoformat(),
                "lt": end_utc.tz_localize(None).isoformat(),
            }
        },
        separators=(",", ":"),
    )
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        payload = get_json(
            _API_URL,
            {
                "resource_id": _RESOURCE_ID,
                "filters": filters,
                "sort": "start_time_utc asc, _id asc",
                "limit": _PAGE_SIZE,
                "offset": offset,
            },
        )
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise ProviderError("Svenska kraftnät returned an invalid API response.")
        result = payload.get("result")
        if not isinstance(result, dict) or result.get("resource_id") != _RESOURCE_ID:
            raise ProviderError("Svenska kraftnät returned an unexpected data resource.")
        fields = result.get("fields")
        if not isinstance(fields, list) or any(not isinstance(field, dict) for field in fields):
            raise ProviderError("Svenska kraftnät returned invalid schema metadata.")
        observed = {field.get("id"): field.get("type") for field in fields}
        if any(observed.get(name) != kind for name, kind in _REQUIRED_FIELDS.items()):
            raise ProviderError("Svenska kraftnät changed the physical-flow schema.")
        page = result.get("records")
        total = result.get("total")
        if (
            not isinstance(page, list)
            or any(not isinstance(row, dict) for row in page)
            or not isinstance(total, int)
            or total < len(rows) + len(page)
        ):
            raise ProviderError("Svenska kraftnät returned invalid pagination metadata.")
        rows.extend(page)
        if len(rows) >= total:
            return rows
        if not page:
            raise ProviderError("Svenska kraftnät returned an incomplete result page.")
        offset += len(page)


def _parse_rows(rows: list[dict[str, Any]], zone: str) -> pd.DataFrame:
    if any(set(_REQUIRED_FIELDS).difference(row) for row in rows):
        raise ProviderError("Svenska kraftnät flow data is missing required fields.")

    selected = [
        row for row in rows if row["from_bidding_zone"] == zone or row["to_bidding_zone"] == zone
    ]
    if not selected:
        frame = pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name="utc_time"))
    else:
        timestamps = pd.to_datetime(
            [row["start_time_utc"] for row in selected], utc=True, errors="coerce"
        )
        if timestamps.hasnans or any(
            timestamp.minute % 15 or timestamp.second or timestamp.microsecond
            for timestamp in timestamps
        ):
            raise ProviderError("Svenska kraftnät returned an invalid 15-minute timestamp.")
        directed = [
            (timestamp, row["from_bidding_zone"], row["to_bidding_zone"])
            for timestamp, row in zip(timestamps, selected)
        ]
        if pd.Index(directed).duplicated().any():
            raise ProviderError("Svenska kraftnät returned duplicate physical-flow records.")
        if any(row["power_unit"] != "MW" for row in selected):
            raise ProviderError("Svenska kraftnät returned an unexpected power unit.")
        power = pd.to_numeric([row["power"] for row in selected], errors="coerce")
        if pd.isna(power).any():
            raise ProviderError("Svenska kraftnät returned a non-numeric flow value.")

        tidy: list[dict[str, Any]] = []
        for timestamp, row, value in zip(timestamps, selected, power):
            exporting = row["from_bidding_zone"] == zone
            counterparty = row["to_bidding_zone"] if exporting else row["from_bidding_zone"]
            if not isinstance(counterparty, str) or not _ZONE.fullmatch(counterparty):
                raise ProviderError("Svenska kraftnät returned an invalid bidding zone.")
            tidy.append(
                {
                    "utc_time": timestamp,
                    "border": f"flow_{counterparty.lower()}_mw",
                    "power": -value if exporting else value,
                }
            )
        frame = pd.DataFrame(tidy).pivot_table(
            index="utc_time", columns="border", values="power", aggfunc="sum"
        )
        frame.columns.name = None
        frame = normalise_index(frame.sort_index())

    frame.attrs.update(
        {
            "source_url": _SOURCE_URL,
            "documentation_url": _DOCUMENTATION_URL,
            "license": "Creative Commons Attribution 4.0",
            "license_url": _LICENSE_URL,
            "attribution": "Produced by Svenska kraftnät business units.",
            "unit": "MW",
            "resolution": "15 minutes",
            "update_frequency": "daily",
            "coverage_start": "2022-01-01",
            "api_version": "dataset 1.0.0; CKAN Action API v3",
            "authentication": "none",
            "pagination": "offset",
            "page_size": _PAGE_SIZE,
            "query_limit": _PAGE_SIZE,
            "rate_limit": None,
            "sign_convention": "positive=import, negative=export",
        }
    )
    return frame


class SVKProvider(DataProvider):
    """Swedish bidding-zone physical flows published by Svenska kraftnät."""

    def get_prices(
        self, zone: str, start: str | pd.Timestamp, end: str | pd.Timestamp, **kwargs: Any
    ) -> pd.DataFrame:
        raise NotImplementedError("Svenska kraftnät price support is not implemented.")

    def get_load(
        self, zone: str, start: str | pd.Timestamp, end: str | pd.Timestamp, **kwargs: Any
    ) -> pd.DataFrame:
        raise NotImplementedError("Svenska kraftnät load support is not implemented.")

    def get_generation(
        self, zone: str, start: str | pd.Timestamp, end: str | pd.Timestamp, **kwargs: Any
    ) -> pd.DataFrame:
        raise NotImplementedError("Svenska kraftnät generation support is not implemented.")

    def get_physical_flows(
        self, zone: str, start: str | pd.Timestamp, end: str | pd.Timestamp, **kwargs: Any
    ) -> pd.DataFrame:
        zone = zone.upper()
        if zone not in _ZONES:
            raise ValueError(f"Svenska kraftnät only covers {sorted(_ZONES)}; got {zone!r}.")
        return _parse_rows(_fetch_rows(start, end), zone)

    def capabilities(self) -> set[str]:
        return {"physical_flows"}

    def zones(self) -> set[str]:
        return set(_ZONES)

    def name(self) -> str:
        return "Svenska kraftnät"


register_provider("svk", SVKProvider())
