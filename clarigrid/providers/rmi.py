"""RMI (Royal Meteorological Institute of Belgium) provider.

Also known as KMI (Dutch) and IRM (French).
No API key required. Open OGC WFS 2.0, CC BY 4.0.
Self-registers on import as ``"rmi"``.

Coverage:
    - **SYNOP** (``synop:synop_data``): 22 synoptic stations, 3-hourly observations.
    - **AWS** (``aws:aws_10min``): Automatic weather stations, 10-minute observations.
      Currently Zeebrugge (code 6455) and Humain are publicly available.

Usage::

    import clarigrid as cg
    cg.connect("rmi")

    # SYNOP — Uccle station (Brussels reference station)
    df = cg.get_weather("6447", "2026-04-01", "2026-05-01")

    # AWS — Zeebrugge (10-min resolution)
    df = cg.get_weather("6455", "2026-05-01", "2026-05-07", dataset="aws")

Zone / station codes:
    SYNOP examples: ``"6447"`` = Uccle, ``"6410"`` = Koksijde, ``"6477"`` = Deurne.
    AWS examples: ``"6455"`` = Zeebrugge.

Dataset (``dataset`` kwarg):
    ``"synop"`` (default, 3-hourly) or ``"aws"`` (10-min).
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from clarigrid.core.http import get_json
from clarigrid.core.interface import WeatherDataProvider
from clarigrid.core.registry import register_provider
from clarigrid.core.types import STANDARD_TZ
from clarigrid.utils.time import normalise_index

_WFS_SYNOP = "https://opendata.meteo.be/service/wfs"
_WFS_AWS = "https://opendata.meteo.be/service/aws/ows"

# The opendata.meteo.be GeoServer does NOT support STARTINDEX with CQL_FILTER
# (returns 400). Instead we use a high COUNT and chunk large date ranges.
_MAX_COUNT = 10_000
_AWS_CHUNK_DAYS = 60   # AWS 10-min data: ~8,640 records / 60 days → under limit


def _wfs_params(typename: str, cql_filter: str) -> dict[str, Any]:
    return {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "TYPENAMES": typename,
        "CQL_FILTER": cql_filter,
        "OUTPUTFORMAT": "application/json",
        "COUNT": _MAX_COUNT,
    }


def _fetch_wfs(url: str, typename: str, cql_filter: str) -> list[dict]:
    """Fetch all features for a WFS CQL filter (no STARTINDEX pagination).

    The opendata.meteo.be server rejects any STARTINDEX > 0 with CQL_FILTER.
    We rely on COUNT=10_000 to capture a full query window in one shot.
    """
    data = get_json(url, _wfs_params(typename, cql_filter))
    features = data.get("features", [])
    return [f["properties"] for f in features if f.get("properties")]


def _fetch_chunked(
    url: str,
    typename: str,
    code: str,
    start: str,
    end: str,
    chunk_days: int,
) -> list[dict]:
    """Fetch by splitting the date range into chunks to stay within COUNT limit."""
    s = pd.Timestamp(start)
    e = pd.Timestamp(end) + pd.Timedelta(days=1)
    records: list[dict] = []
    chunk_start = s
    while chunk_start < e:
        chunk_end = min(chunk_start + pd.Timedelta(days=chunk_days), e)
        cs = chunk_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        ce = chunk_end.strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            int(code)
            station_filter = f"code={code}"
        except ValueError:
            station_filter = f"code='{code}'"
        cql = f"{station_filter} AND timestamp DURING {cs}/{ce}"
        records.extend(_fetch_wfs(url, typename, cql))
        chunk_start = chunk_end
    return records


def _cql_filter(code: str, start: str, end: str) -> str:
    """Build CQL_FILTER for station code + time range."""
    s = pd.Timestamp(start).strftime("%Y-%m-%dT%H:%M:%SZ")
    e = (pd.Timestamp(end) + pd.Timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        int(code)
        station_filter = f"code={code}"
    except ValueError:
        station_filter = f"code='{code}'"
    return f"{station_filter} AND timestamp DURING {s}/{e}"


# SYNOP field → standard column name.
_SYNOP_RENAME: dict[str, str] = {
    "temp": "temperature_c",
    "temp_min": "temperature_min_c",
    "temp_max": "temperature_max_c",
    "temp_grass_min": "temperature_grass_min_c",
    "wind_speed": "wind_speed_ms",
    "wind_direction": "wind_direction_deg",
    "wind_peak_speed": "wind_gust_ms",
    "humidity_relative": "humidity_pct",
    "pressure": "pressure_hpa",
    "pressure_station_level": "pressure_station_hpa",
    "precip_quantity": "precipitation_mm",
    "sun_duration_24hours": "sunshine_s_24h",
    "short_wave_from_sky_24hours": "solar_radiation_j_m2",
    "cloudiness": "cloud_cover_oktas",
}

# AWS field → standard column name.
_AWS_RENAME: dict[str, str] = {
    "temp_dry_shelter_avg": "temperature_c",
    "humidity_rel_shelter_avg": "humidity_pct",
    "pressure": "pressure_hpa",
    "wind_speed_10m": "wind_speed_ms",
    "wind_gusts_speed": "wind_gust_ms",
    "wind_direction": "wind_direction_deg",
    "short_wave_from_sky_avg": "shortwave_radiation_w_m2",
    "sun_duration": "sunshine_s",
    "precip_quantity": "precipitation_mm",
    "temp_soil_avg_5cm": "soil_temp_5cm_c",
    "temp_soil_avg_10cm": "soil_temp_10cm_c",
    "temp_soil_avg_20cm": "soil_temp_20cm_c",
    "temp_soil_avg_50cm": "soil_temp_50cm_c",
}


def _records_to_df(records: list[dict], rename_map: dict[str, str]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()

    rows: list[dict] = []
    for r in records:
        ts_raw = r.get("timestamp")
        if not ts_raw:
            continue
        try:
            ts = pd.Timestamp(ts_raw)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            else:
                ts = ts.tz_convert("UTC")
        except Exception:
            continue

        row: dict = {"utc_time": ts}
        for src, dst in rename_map.items():
            val = r.get(src)
            if val is not None:
                try:
                    row[dst] = float(val)
                except (TypeError, ValueError):
                    row[dst] = float("nan")
        rows.append(row)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows).set_index("utc_time")
    df.index.name = "utc_time"
    # Drop duplicate timestamps (can occur near DST transitions).
    if df.index.duplicated().any():
        df = df[~df.index.duplicated(keep="first")]
    return df


class RmiProvider(WeatherDataProvider):
    """RMI Belgium — SYNOP synoptic network and AWS automatic weather stations."""

    def get_weather(
        self,
        zone: str,
        start: str,
        end: str,
        *,
        dataset: str = "synop",
        **kwargs,
    ) -> pd.DataFrame:
        """Return weather observations from RMI Belgium.

        Args:
            zone: Numeric station code (e.g. ``"6447"`` for Uccle).
            start: Start date (ISO format).
            end: End date (ISO format).
            dataset: ``"synop"`` (default, 3-hourly) or ``"aws"`` (10-min).

        Returns:
            DataFrame with UTC ``DatetimeIndex``. See module docstring for columns.
        """
        if dataset.lower() == "aws":
            # AWS 10-min has ~144 records/day — chunk to stay within COUNT limit.
            records = _fetch_chunked(
                _WFS_AWS, "aws:aws_10min", zone, start, end, _AWS_CHUNK_DAYS
            )
            df = _records_to_df(records, _AWS_RENAME)
        else:
            # SYNOP 3-hourly: ~8/day, no chunking needed for typical ranges.
            cql = _cql_filter(zone, start, end)
            records = _fetch_wfs(_WFS_SYNOP, "synop:synop_data", cql)
            df = _records_to_df(records, _SYNOP_RENAME)

        if df.empty:
            return df
        return normalise_index(df, STANDARD_TZ)

    def name(self) -> str:
        return "RMI Belgium (opendata.meteo.be)"


def rmi_station_list(dataset: str = "synop") -> pd.DataFrame:
    """Fetch available RMI station codes and names.

    Args:
        dataset: ``"synop"`` or ``"aws"``.

    Returns:
        DataFrame with columns ``code`` and any other available metadata.
    """
    if dataset.lower() == "aws":
        typename = "aws:aws_station" if dataset else "aws:aws_station"
        url = _WFS_AWS
    else:
        typename = "synop:synop_station"
        url = _WFS_SYNOP

    params: dict[str, Any] = {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "TYPENAMES": typename,
        "OUTPUTFORMAT": "application/json",
        "COUNT": 500,
    }
    data = get_json(url, params)
    rows = []
    for f in data.get("features", []):
        p = f.get("properties", {})
        if p:
            rows.append(p)
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def register() -> None:
    """Register the RMI provider. Called automatically on import."""
    register_provider("rmi", RmiProvider())


register()
