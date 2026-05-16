"""SMARD (Bundesnetzagentur) provider — Germany electricity.

No API key required. Covers DE, AT, LU and TSO sub-zones.
Self-registers on import as ``"smard"``.

Usage::

    import clarigrid as cg
    cg.connect("smard")
    df = cg.get_prices("DE", "2025-01-01", "2025-01-07")

Supported zones: ``"DE"`` (default), ``"AT"``, ``"LU"``,
``"50hertz"``, ``"amprion"``, ``"tennet"``, ``"transnetbw"``.
"""

from __future__ import annotations

import pandas as pd

from clarigrid.core.http import get_json
from clarigrid.core.interface import DataProvider
from clarigrid.core.registry import register_provider
from clarigrid.core.types import COLUMN_LOAD, COLUMN_PRICE, STANDARD_TZ
from clarigrid.utils.time import normalise_index, parse_dt

_BASE = "https://www.smard.de/app/chart_data"

_FILTER_PRICE = 4169
_FILTER_LOAD = 4066

_GENERATION_FILTERS: dict[str, int] = {
    "solar_mwh": 1223,
    "wind_offshore_mwh": 1224,
    "wind_onshore_mwh": 1225,
    "lignite_mwh": 1226,
    "hard_coal_mwh": 1227,
    "gas_mwh": 1228,
    "nuclear_mwh": 1229,
}

_REGION_MAP: dict[str, str] = {
    "DE": "DE",
    "DE_LU": "DE",
    "AT": "AT",
    "LU": "LU",
    "50HERTZ": "50hertz",
    "AMPRION": "amprion",
    "TENNET": "tennet",
    "TRANSNETBW": "transnetbw",
}

_WEEK_MS = 7 * 24 * 3600 * 1000


def _resolve_region(zone: str) -> str:
    return _REGION_MAP.get(zone.upper().replace("-", "_"), zone.lower())


def _get_index(filter_code: int, region: str, resolution: str) -> list[int]:
    """Fetch available week-start timestamps for a filter/region/resolution."""
    url = f"{_BASE}/{filter_code}/{region}/index_{resolution}.json"
    data = get_json(url)
    if isinstance(data, list):
        return data
    # Some responses wrap in {"timestamps": [...]}
    return data.get("timestamps", [])


def _get_week(filter_code: int, region: str, resolution: str, ts_ms: int) -> list[list]:
    """Fetch series data for one week."""
    url = f"{_BASE}/{filter_code}/{region}/{filter_code}_{region}_{resolution}_{ts_ms}.json"
    data = get_json(url)
    return data.get("series", [])


def _fetch_series(
    filter_code: int,
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    resolution: str = "hour",
) -> pd.Series:
    """Return a Series indexed by UTC timestamp for the given filter and range."""
    region = _resolve_region(zone)
    start_ts = parse_dt(start)
    end_ts = parse_dt(end)
    start_ms = int(start_ts.timestamp() * 1000)
    end_ms = int(end_ts.timestamp() * 1000)

    index_timestamps = _get_index(filter_code, region, resolution)
    # Keep weeks that overlap the requested range.
    relevant = [
        ts for ts in index_timestamps
        if ts <= end_ms and ts + _WEEK_MS > start_ms
    ]

    all_points: list[list] = []
    for ts in relevant:
        all_points.extend(_get_week(filter_code, region, resolution, ts))

    if not all_points:
        return pd.Series(dtype=float, name="value")

    # Series is [[timestamp_ms, value_or_null], ...]
    df = pd.DataFrame(all_points, columns=["ts_ms", "value"])
    df["ts_ms"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    df = df.set_index("ts_ms")
    df.index.name = "utc_time"
    df["value"] = pd.to_numeric(df["value"], errors="coerce")  # null → NaN

    # Trim to exact requested range.
    return df["value"].loc[start_ts:end_ts]


class SmardProvider(DataProvider):
    """Bundesnetzagentur SMARD data portal — Germany electricity."""

    def get_prices(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        s = _fetch_series(_FILTER_PRICE, zone, start, end)
        return normalise_index(s.rename(COLUMN_PRICE).to_frame(), STANDARD_TZ)

    def get_load(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        s = _fetch_series(_FILTER_LOAD, zone, start, end)
        return normalise_index(s.rename(COLUMN_LOAD).to_frame(), STANDARD_TZ)

    def get_generation(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        frames: dict[str, pd.Series] = {}
        for col, code in _GENERATION_FILTERS.items():
            try:
                s = _fetch_series(code, zone, start, end)
                if not s.empty:
                    frames[col] = s
            except Exception:
                # Some fuel types unavailable for certain regions — skip silently.
                pass

        if not frames:
            return pd.DataFrame()

        df = pd.concat(frames, axis=1)
        df.columns = list(frames.keys())
        return normalise_index(df, STANDARD_TZ)

    def capabilities(self) -> set[str]:
        return {"prices", "load", "generation"}

    def name(self) -> str:
        return "SMARD (Bundesnetzagentur)"


def register() -> None:
    """Register the SMARD provider. Called automatically on import."""
    register_provider("smard", SmardProvider())


register()
