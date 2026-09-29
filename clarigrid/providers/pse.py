"""Polish power-system load from Polskie Sieci Elektroenergetyczne (PSE)."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urlencode

import pandas as pd

from clarigrid.core.exceptions import ProviderError
from clarigrid.core.http import get_json
from clarigrid.core.interface import DataProvider
from clarigrid.core.registry import register_provider
from clarigrid.utils.time import normalise_index

_BASE = "https://api.raporty.pse.pl/api"
_ENDPOINT = f"{_BASE}/kse-load"
_SOURCE_URL = "https://raporty.pse.pl/?report=PDGSZ"
_DOCUMENTATION_URL = f"{_BASE}/openapi"
_LICENSE_URL = "https://www.pse.pl/bip/ponowne-wykorzystanie-informacji-publicznej"
_LOCAL_TZ = "Europe/Warsaw"
_PAGE_SIZE = 100  # OpenAPI documents 100 as the default; no maximum is published.
_FIELDS = {
    "load_actual": "load_mw",
    "load_fcst": "load_forecast_mw",
}
_PERIOD = re.compile(r"^(\d{2}):(\d{2}) - (\d{2}):(\d{2})$")


def _empty_frame(column: str) -> pd.DataFrame:
    return pd.DataFrame(
        {column: pd.Series(dtype=float)},
        index=pd.DatetimeIndex([], tz="UTC", name="utc_time"),
    )


def _local_time(value: str | pd.Timestamp) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return pd.Timestamp(timestamp.tz_localize(_LOCAL_TZ))
    return pd.Timestamp(timestamp.tz_convert(_LOCAL_TZ))


def _utc_bounds(
    start: str | pd.Timestamp, end: str | pd.Timestamp
) -> tuple[pd.Timestamp, pd.Timestamp]:
    start_local = _local_time(start)
    end_local = _local_time(end)
    if end_local == end_local.normalize():
        end_local += pd.DateOffset(days=1)
    if end_local <= start_local:
        end_local = start_local + pd.DateOffset(days=1)
    return start_local.tz_convert("UTC"), end_local.tz_convert("UTC")


def _query_url(start: str | pd.Timestamp, end: str | pd.Timestamp, field: str) -> str:
    start_utc, end_utc = _utc_bounds(start, end)
    fmt = "%Y-%m-%d %H:%M:%S"
    params = {
        "$select": f"dtime_utc,period_utc,{field},publication_ts_utc",
        "$filter": (
            f"dtime_utc ge '{start_utc.strftime(fmt)}' and "
            f"dtime_utc lt '{end_utc.strftime(fmt)}'"
        ),
        "$orderby": "dtime_utc asc",
        "$first": _PAGE_SIZE,
    }
    query = urlencode(params, quote_via=quote).replace("%24", "$")
    return f"{_ENDPOINT}?{query}"


def _fetch_rows(
    start: str | pd.Timestamp, end: str | pd.Timestamp, field: str
) -> list[dict[str, Any]]:
    url: str | None = _query_url(start, end, field)
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    while url:
        if url in seen:
            raise ProviderError("PSE returned a repeated pagination cursor.")
        seen.add(url)
        payload = get_json(url)
        if not isinstance(payload, dict) or not isinstance(payload.get("value"), list):
            raise ProviderError("PSE returned an invalid load response.")
        page = payload["value"]
        if any(not isinstance(row, dict) for row in page):
            raise ProviderError("PSE returned an invalid load record.")
        rows.extend(page)
        next_link = payload.get("nextLink")
        if next_link is None:
            break
        if not isinstance(next_link, str) or not next_link.startswith(f"{_ENDPOINT}?"):
            raise ProviderError("PSE returned an invalid pagination link.")
        url = next_link
    return rows


def _interval_minutes(period: Any) -> int:
    match = _PERIOD.fullmatch(str(period))
    if not match:
        raise ProviderError(f"PSE returned an invalid UTC period: {period!r}.")
    start_hour, start_minute, end_hour, end_minute = map(int, match.groups())
    if start_hour > 23 or end_hour > 24 or start_minute > 59 or end_minute > 59:
        raise ProviderError(f"PSE returned an invalid UTC period: {period!r}.")
    minutes = (end_hour * 60 + end_minute - start_hour * 60 - start_minute) % 1440
    if minutes != 15:
        raise ProviderError(f"PSE returned an unexpected {minutes}-minute interval.")
    return minutes


def _parse_rows(rows: list[dict[str, Any]], field: str) -> pd.DataFrame:
    column = _FIELDS[field]
    if not rows:
        frame = _empty_frame(column)
    else:
        required = {"dtime_utc", "period_utc", field}
        if any(required.difference(row) for row in rows):
            raise ProviderError("PSE load response is missing required fields.")
        intervals = [_interval_minutes(row["period_utc"]) for row in rows]
        timestamps = pd.to_datetime(
            [row["dtime_utc"] for row in rows], utc=True, errors="coerce"
        )
        if timestamps.hasnans:
            raise ProviderError("PSE returned an invalid UTC timestamp.")
        values = pd.to_numeric([row[field] for row in rows], errors="coerce")
        frame = pd.DataFrame(
            {column: values},
            index=timestamps - pd.to_timedelta(intervals, unit="m"),
        )
        frame = normalise_index(frame.groupby(level=0).last().sort_index())

    frame.attrs.update({
        "source_url": _SOURCE_URL,
        "documentation_url": _DOCUMENTATION_URL,
        "license": "PSE public-sector information reuse terms",
        "license_url": _LICENSE_URL,
        "reuse_notice": (
            "Informacja pozyskana ze strony www.pse.pl, wg. stanu strony na dzień "
            "[fetched_at], przetworzona w części."
        ),
        "transformation": "15-minute interval-end timestamps shifted to interval starts",
        "unit": "MW",
        "resolution": "15 minutes",
        "api_version": "2.0.12",
        "authentication": "none",
        "page_size": _PAGE_SIZE,
    })
    publication_values = [
        value
        for row in rows
        if isinstance(value := row.get("publication_ts_utc"), str)
    ]
    published = pd.Series(pd.to_datetime(publication_values, utc=True, errors="coerce"))
    if published.notna().any():
        frame.attrs["data_updated_at"] = published.max().isoformat()
    return frame


def _fetch(
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    field: str,
) -> pd.DataFrame:
    if zone.upper() != "PL":
        raise ValueError(f"PSE only covers zone 'PL'; got {zone!r}.")
    return _parse_rows(_fetch_rows(start, end, field), field)


class PSEProvider(DataProvider):
    """Actual and forecast Polish power-system load from PSE API v2."""

    def get_prices(
        self, zone: str, start: str | pd.Timestamp, end: str | pd.Timestamp, **kwargs: Any
    ) -> pd.DataFrame:
        raise NotImplementedError("PSE price support is not implemented.")

    def get_load(
        self, zone: str, start: str | pd.Timestamp, end: str | pd.Timestamp, **kwargs: Any
    ) -> pd.DataFrame:
        return _fetch(zone, start, end, "load_actual")

    def get_load_forecast(
        self, zone: str, start: str | pd.Timestamp, end: str | pd.Timestamp, **kwargs: Any
    ) -> pd.DataFrame:
        return _fetch(zone, start, end, "load_fcst")

    def get_generation(
        self, zone: str, start: str | pd.Timestamp, end: str | pd.Timestamp, **kwargs: Any
    ) -> pd.DataFrame:
        raise NotImplementedError("PSE generation support is not implemented.")

    def capabilities(self) -> set[str]:
        return {"load", "load_forecast"}

    def zones(self) -> set[str]:
        return {"PL"}

    def name(self) -> str:
        return "Polskie Sieci Elektroenergetyczne"


register_provider("pse", PSEProvider())
