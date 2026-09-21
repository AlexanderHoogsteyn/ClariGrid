"""Singapore monthly electricity generation from SINGSTAT/data.gov.sg."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from clarigrid.core.exceptions import ProviderError
from clarigrid.core.http import get_json
from clarigrid.core.interface import DataProvider
from clarigrid.core.registry import register_provider
from clarigrid.utils.time import normalise_index, parse_dt

_DATASET_ID = "d_ae4afbaf5bc96bde19d8ce85810ab9f4"
_API_URL = f"https://api-production.data.gov.sg/v2/public/api/datasets/{_DATASET_ID}/list-rows"
_SOURCE_URL = f"https://data.gov.sg/datasets/{_DATASET_ID}/view"
_DOCUMENTATION_URL = "https://guide.data.gov.sg/developer-guide/dataset-apis/list-rows-of-dataset"
_LICENSE_URL = "https://data.gov.sg/open-data-licence"
_LOCAL_TZ = "Asia/Singapore"


def _generation_frame(
    payload: dict[str, Any],
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
) -> pd.DataFrame:
    """Convert monthly GWh totals to average MW at each Singapore month start."""
    if payload.get("code") != 0:
        raise ProviderError(
            f"data.gov.sg returned an error: {payload.get('errorMsg') or 'unknown error'}"
        )
    rows = payload.get("data", {}).get("rows", [])
    record = next(
        (row for row in rows if row.get("DataSeries") == "Electricity Generation"),
        None,
    )
    if record is None:
        raise ProviderError("SINGSTAT response is missing the Electricity Generation series.")

    requested_start, requested_end = parse_dt(start), parse_dt(end)
    values: list[tuple[pd.Timestamp, float]] = []
    for field, raw_value in record.items():
        try:
            month = datetime.strptime(field, "%Y%b")
        except (TypeError, ValueError):
            continue
        local_start = pd.Timestamp(month, tz=_LOCAL_TZ)
        local_end = local_start + pd.offsets.MonthBegin()
        utc_start = local_start.tz_convert("UTC")
        utc_end = local_end.tz_convert("UTC")
        if utc_end <= requested_start or utc_start >= requested_end:
            continue
        gwh = pd.to_numeric(raw_value, errors="coerce")
        if pd.isna(gwh):
            continue
        hours = (utc_end - utc_start).total_seconds() / 3600
        values.append((utc_start, float(gwh) * 1_000 / hours))

    if not values:
        return pd.DataFrame(
            index=pd.DatetimeIndex([], tz="UTC", name="utc_time"),
            columns=["total_generation_mw"],
            dtype=float,
        )
    index, generation = zip(*sorted(values))
    return normalise_index(
        pd.DataFrame(
            {"total_generation_mw": generation},
            index=pd.DatetimeIndex(index),
        )
    )


class SingstatProvider(DataProvider):
    """SINGSTAT energy statistics published through Singapore's open-data API."""

    def get_generation(
        self,
        zone: str,
        start: str | pd.Timestamp,
        end: str | pd.Timestamp,
        **kwargs: Any,
    ) -> pd.DataFrame:
        frame = _generation_frame(get_json(_API_URL), start, end)
        frame.attrs.update(
            {
                "source_url": _SOURCE_URL,
                "documentation_url": _DOCUMENTATION_URL,
                "dataset_id": _DATASET_ID,
                "license": "Singapore Open Data Licence 1.0",
                "license_url": _LICENSE_URL,
                "unit": "MW",
                "source_unit": "GWh/month",
                "resolution": "monthly average",
                "update_frequency": "monthly",
            }
        )
        return frame

    def get_prices(
        self,
        zone: str,
        start: str | pd.Timestamp,
        end: str | pd.Timestamp,
        **kwargs: Any,
    ) -> pd.DataFrame:
        raise NotImplementedError("SINGSTAT does not publish wholesale electricity prices.")

    def get_load(
        self,
        zone: str,
        start: str | pd.Timestamp,
        end: str | pd.Timestamp,
        **kwargs: Any,
    ) -> pd.DataFrame:
        raise NotImplementedError(
            "SINGSTAT electricity consumption is an energy total, not system load."
        )

    def capabilities(self) -> set[str]:
        return {"generation"}

    def zones(self) -> set[str]:
        return {"SG"}

    def name(self) -> str:
        return "Singapore Department of Statistics"


register_provider("singstat", SingstatProvider())
