"""Irish power-system demand from EirGrid's Smart Grid Dashboard."""

from __future__ import annotations

from datetime import datetime
from numbers import Real
from typing import Any

import pandas as pd

from clarigrid.core.exceptions import ProviderError
from clarigrid.core.http import get_json
from clarigrid.core.interface import DataProvider
from clarigrid.core.registry import register_provider
from clarigrid.utils.time import normalise_index, parse_dt

_ENDPOINT = "https://www.smartgriddashboard.com/api/chart/"
_SOURCE_URL = "https://www.smartgriddashboard.com/all/demand/"
_DOCUMENTATION_URL = (
    "https://cms.eirgrid.ie/sites/default/files/2024-11/"
    "CleanerGrid-Extra-Information-Document-November-2024.pdf"
)
_LICENSE_URL = "https://www.smartgriddashboard.com/all/open-data-license/"
_REGIONS = {"IE": "ROI", "GB_NIR": "NI"}
_FIELD = "SYSTEM_DEMAND"


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {"load_mw": pd.Series(dtype=float)},
        index=pd.DatetimeIndex([], tz="UTC", name="utc_time"),
    )


def _bounds(
    start: str | pd.Timestamp, end: str | pd.Timestamp
) -> tuple[pd.Timestamp, pd.Timestamp]:
    start_utc = pd.Timestamp(parse_dt(start))
    end_utc = pd.Timestamp(parse_dt(end))
    if end_utc == end_utc.normalize():
        end_utc += pd.Timedelta(days=1)
    if end_utc <= start_utc:
        raise ValueError("EirGrid end must be after start.")
    return start_utc, end_utc


def _parse_timestamp(value: Any) -> pd.Timestamp:
    if not isinstance(value, str):
        raise ProviderError("EirGrid returned an invalid UTC timestamp.")
    try:
        return pd.Timestamp(datetime.strptime(value, "%d-%b-%Y %H:%M:%S"), tz="UTC")
    except ValueError as exc:
        raise ProviderError("EirGrid returned an invalid UTC timestamp.") from exc


def _parse_response(
    payload: Any,
    region: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    if not isinstance(payload, dict) or not isinstance(payload.get("Rows"), list):
        raise ProviderError("EirGrid returned an invalid demand response.")

    records: list[tuple[pd.Timestamp, float]] = []
    all_timestamps: list[pd.Timestamp] = []
    required = {"EffectiveTime", "FieldName", "Region", "Value"}
    for row in payload["Rows"]:
        if not isinstance(row, dict) or required.difference(row):
            raise ProviderError("EirGrid demand response is missing required fields.")
        if row["FieldName"] != _FIELD or row["Region"] != region:
            raise ProviderError("EirGrid returned an unexpected field or region.")
        timestamp = _parse_timestamp(row["EffectiveTime"])
        all_timestamps.append(timestamp)
        value = row["Value"]
        if value is None:
            continue
        if not isinstance(value, Real) or isinstance(value, bool):
            raise ProviderError("EirGrid returned a non-numeric demand value.")
        records.append((timestamp, float(value)))

    ordered = pd.DatetimeIndex(sorted(all_timestamps))
    if ordered.has_duplicates:
        raise ProviderError("EirGrid returned duplicate demand intervals.")
    if len(ordered) > 1:
        interval_seconds = ordered.to_series().diff().dropna().dt.total_seconds()
        if (interval_seconds <= 0).any() or (interval_seconds % 900 != 0).any():
            raise ProviderError("EirGrid returned an unexpected demand interval.")

    if not records:
        frame = _empty_frame()
    else:
        frame = pd.DataFrame(records, columns=["utc_time", "load_mw"])
        frame = frame.set_index("utc_time").sort_index()
        frame = normalise_index(frame.loc[(frame.index >= start) & (frame.index < end)])

    frame.attrs.update(
        {
            "source_url": _SOURCE_URL,
            "documentation_url": _DOCUMENTATION_URL,
            "license": "EirGrid Group Open Data Licence",
            "license_url": _LICENSE_URL,
            "reuse_notice": "Supported by EirGrid Group Data",
            "redistribution": "commercial and non-commercial with attribution",
            "geographic_coverage": "IE, GB-NIR",
            "temporal_coverage": None,
            "update_frequency": "real time",
            "unit": "MW",
            "resolution": "15 minutes",
            "authentication": "none",
            "api_version": None,
            "api_stability": None,
            "pagination": None,
            "query_limit": None,
            "rate_limit": None,
        }
    )
    return frame


def _fetch(
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
) -> pd.DataFrame:
    canonical_zone = zone.upper()
    if canonical_zone not in _REGIONS:
        raise ValueError(f"EirGrid zone must be one of {sorted(_REGIONS)}; got {zone!r}.")
    start_utc, end_utc = _bounds(start, end)
    payload = get_json(
        _ENDPOINT,
        {
            "region": _REGIONS[canonical_zone],
            "chartType": "demand",
            "dateRange": "day",
            "dateFrom": start_utc.strftime("%Y-%m-%d"),
            "dateTo": (end_utc - pd.Timedelta("1ns")).strftime("%Y-%m-%d"),
            "areas": "demandactual",
        },
    )
    return _parse_response(payload, _REGIONS[canonical_zone], start_utc, end_utc)


class EirGridProvider(DataProvider):
    """Fifteen-minute Irish and Northern Irish system demand."""

    def get_prices(
        self, zone: str, start: str | pd.Timestamp, end: str | pd.Timestamp, **kwargs: Any
    ) -> pd.DataFrame:
        raise NotImplementedError("EirGrid price support is not implemented.")

    def get_load(
        self, zone: str, start: str | pd.Timestamp, end: str | pd.Timestamp, **kwargs: Any
    ) -> pd.DataFrame:
        return _fetch(zone, start, end)

    def get_generation(
        self, zone: str, start: str | pd.Timestamp, end: str | pd.Timestamp, **kwargs: Any
    ) -> pd.DataFrame:
        raise NotImplementedError("EirGrid generation support is not implemented.")

    def capabilities(self) -> set[str]:
        return {"load"}

    def zones(self) -> set[str]:
        return set(_REGIONS)

    def name(self) -> str:
        return "EirGrid Smart Grid Dashboard"


register_provider("eirgrid", EirGridProvider())
