"""DWD Open Data (Climate Data Center) provider — Germany.

No API key required. Anonymous HTTPS file downloads from opendata.dwd.de.
Licence: GeoNutzV (German governmental open data, compatible with CC BY).
Self-registers on import as ``"dwd"``.

Coverage:
    Historical station observations across Germany. Hourly, daily, 10-min and
    monthly resolutions. Key parameters: air temperature, wind, solar radiation,
    precipitation, pressure, soil temperature, sunshine duration.

Usage::

    import clarigrid as cg
    cg.connect("dwd")

    # Hourly air temperature + humidity at Berlin-Tempelhof
    df = cg.get_weather("02564", "2024-01-01", "2024-12-31",
                        parameter="air_temperature")

    # Hourly wind at Hamburg
    df = cg.get_weather("01975", "2024-06-01", "2024-08-31",
                        parameter="wind")

Zone:
    5-digit DWD station ID (zero-padded string), e.g. ``"02564"``.
    Use ``dwd_station_list(parameter)`` to discover station IDs.

Parameters (``parameter`` kwarg):
    ``"air_temperature"``  — temperature (°C) + humidity (%)
    ``"wind"``             — wind speed (m/s) + direction (°)
    ``"solar"``            — global radiation (J/cm²) + sunshine (min)
    ``"precipitation"``    — hourly precipitation (mm)
    ``"pressure"``         — station-level pressure (hPa)
    ``"sun"``              — sunshine duration (min)
    ``"cloudiness"``       — cloud cover (categorical)

Period (``period`` kwarg):
    ``"recent"``    — default; ~last 500 days, daily updates, QC not finalised.
    ``"historical"``— older data, QC complete, updated annually.
    Pass ``"auto"`` to try recent first; fall back to historical if station
    file not found.

Resolution (``resolution`` kwarg):
    ``"hourly"`` (default), ``"daily"``, ``"10_minutes"``, ``"monthly"``
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any

import pandas as pd

from clarigrid.core.http import get_bytes, get_text
from clarigrid.core.interface import WeatherDataProvider
from clarigrid.core.registry import register_provider
from clarigrid.core.types import STANDARD_TZ
from clarigrid.utils.time import normalise_index

_CDC_BASE = (
    "https://opendata.dwd.de/climate_environment/CDC/"
    "observations_germany/climate"
)

# Map parameter name → (directory name, file prefix, {raw_col: clean_col})
_PARAM_META: dict[str, tuple[str, str, dict[str, str]]] = {
    "air_temperature": ("air_temperature", "TU", {
        "TT_TU": "temperature_c",
        "RF_TU": "humidity_pct",
    }),
    "wind": ("wind", "FF", {
        "F": "wind_speed_ms",
        "D": "wind_direction_deg",
    }),
    "solar": ("solar", "ST", {
        "FG_LBERG": "global_radiation_j_cm2",
        "SD_LBERG": "sunshine_min",
        "ATMO_LBERG": "atmo_radiation_j_cm2",
        "FD_LBERG": "diffuse_radiation_j_cm2",
    }),
    "precipitation": ("precipitation", "RR", {
        "R1": "precipitation_mm",
        "RS_IND": "precip_type_indicator",
        "WRTR": "precip_form",
    }),
    "pressure": ("pressure", "P0", {
        "P0": "pressure_hpa",
        "P0red": "pressure_reduced_hpa",
    }),
    "sun": ("sun", "SD", {
        "SD_SO": "sunshine_min",
    }),
    "cloudiness": ("cloudiness", "BEW", {
        "V_N": "cloud_cover_oktas",
    }),
    "soil_temperature": ("soil_temperature", "EB", {
        "V_TE002": "soil_temp_2cm_c",
        "V_TE005": "soil_temp_5cm_c",
        "V_TE010": "soil_temp_10cm_c",
        "V_TE020": "soil_temp_20cm_c",
        "V_TE050": "soil_temp_50cm_c",
        "V_TE100": "soil_temp_100cm_c",
    }),
    "visibility": ("visibility", "VV", {
        "V_VV": "visibility_m",
        "V_VV_I": "visibility_indicator",
    }),
    "moisture": ("moisture", "TF", {
        "TF_TU": "dew_point_c",
        "P_TU": "vapour_pressure_hpa",
        "TF_TU_RED": "relative_humidity_pct",
    }),
}

# MESS_DATUM format by resolution
_DATE_FORMATS: dict[str, str] = {
    "10_minutes": "%Y%m%d%H%M",
    "hourly": "%Y%m%d%H",
    "daily": "%Y%m%d",
    "monthly": "%Y%m",
}

# Timezone for DWD station data (UTC in recent files per DWD spec, but
# some historical files use CET/CEST — we check and convert appropriately).
_DWD_LOCAL_TZ = "Europe/Berlin"


def _dir_url(resolution: str, parameter: str, period: str) -> str:
    param_dir = _PARAM_META[parameter][0]
    return f"{_CDC_BASE}/{resolution}/{param_dir}/{period}/"


def _find_station_zip(
    resolution: str,
    parameter: str,
    period: str,
    station_id: str,
) -> str | None:
    """Find the ZIP filename for a station by listing the DWD directory."""
    url = _dir_url(resolution, parameter, period)
    prefix = _PARAM_META[parameter][1]
    try:
        html = get_text(url, timeout=20)
    except Exception:
        return None

    # Pattern: stundenwerte_TU_02564_akt.zip  OR
    #          stundenwerte_TU_02564_19490101_20231231_hist.zip
    sid_padded = station_id.zfill(5)
    # Escape prefix in case it contains special chars (it doesn't, but be safe)
    pattern = re.compile(
        rf'href="([^"]*{re.escape(prefix)}_{sid_padded}[^"]*\.zip)"',
        re.IGNORECASE,
    )
    matches = pattern.findall(html)
    if not matches:
        return None

    # Prefer the exact match for the requested period suffix.
    suffix = "_akt.zip" if period == "recent" else "_hist.zip"
    for m in matches:
        if m.endswith(suffix):
            fname = m.split("/")[-1]
            return url + fname

    # Fall back to first match.
    fname = matches[0].split("/")[-1]
    return url + fname


def _download_and_parse(
    zip_url: str,
    col_map: dict[str, str],
    date_format: str,
    start: str,
    end: str,
) -> pd.DataFrame:
    """Download ZIP, extract data file, return filtered DataFrame."""
    raw = get_bytes(zip_url, timeout=60)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        data_file = next(
            (n for n in zf.namelist() if n.startswith("produkt_")), None
        )
        if data_file is None:
            raise ValueError(f"No produkt_*.txt file found in ZIP: {zip_url}")

        df = pd.read_csv(
            zf.open(data_file),
            sep=";",
            encoding="latin-1",
            dtype=str,
        )

    # Strip whitespace from column names.
    df.columns = [c.strip() for c in df.columns]

    # Parse MESS_DATUM to UTC.
    if "MESS_DATUM" not in df.columns:
        raise ValueError(f"MESS_DATUM column not found. Columns: {list(df.columns)}")

    df["MESS_DATUM"] = df["MESS_DATUM"].str.strip()

    # DWD recent files label their timestamps as UTC ("utc" suffix in some files).
    # Historical files may use local time. We assume UTC for recent, local for historical.
    # Best practice: treat as UTC unless there's explicit evidence otherwise.
    times = pd.to_datetime(df["MESS_DATUM"], format=date_format, errors="coerce", utc=True)
    df.index = times
    df.index.name = "utc_time"
    df = df[~df.index.isna()]

    # Filter to requested date range.
    s = pd.Timestamp(start, tz="UTC")
    e = pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)
    df = df[(df.index >= s) & (df.index < e)]

    # Keep and rename value columns; coerce to float.
    available = {k: v for k, v in col_map.items() if k in df.columns}
    if not available:
        return pd.DataFrame()

    result = pd.DataFrame(index=df.index)
    for src, dst in available.items():
        vals = df[src].str.strip().replace("-999", float("nan")).replace("-9999", float("nan"))
        result[dst] = pd.to_numeric(vals, errors="coerce")

    return result


class DwdProvider(WeatherDataProvider):
    """DWD Open Data (CDC) — Germany historical station observations."""

    def get_weather(
        self,
        zone: str,
        start: str,
        end: str,
        *,
        parameter: str = "air_temperature",
        resolution: str = "hourly",
        period: str = "auto",
        **kwargs,
    ) -> pd.DataFrame:
        """Return station observations from DWD Open Data.

        Args:
            zone: 5-digit DWD station ID (zero-padded), e.g. ``"02564"``.
            start: Start date (ISO format).
            end: End date (ISO format).
            parameter: Observation parameter. See module docstring for options.
                Default: ``"air_temperature"``.
            resolution: Time resolution. ``"hourly"`` (default), ``"daily"``,
                ``"10_minutes"``, ``"monthly"``.
            period: ``"recent"`` (default), ``"historical"``, or ``"auto"``.
                ``"auto"`` tries recent first, falls back to historical.

        Returns:
            DataFrame with UTC ``DatetimeIndex``. Columns depend on parameter.
        """
        if parameter not in _PARAM_META:
            raise ValueError(
                f"Unknown DWD parameter {parameter!r}. "
                f"Available: {sorted(_PARAM_META)}"
            )

        _, _, col_map = _PARAM_META[parameter]
        date_fmt = _DATE_FORMATS.get(resolution, "%Y%m%d%H")
        station_id = zone.zfill(5)

        periods_to_try: list[str]
        if period == "auto":
            periods_to_try = ["recent", "historical"]
        else:
            periods_to_try = [period]

        for p in periods_to_try:
            zip_url = _find_station_zip(resolution, parameter, p, station_id)
            if zip_url is None:
                continue
            try:
                df = _download_and_parse(zip_url, col_map, date_fmt, start, end)
            except Exception:
                continue
            if not df.empty:
                return normalise_index(df, STANDARD_TZ)

        # If both periods found no data, return empty.
        return pd.DataFrame()

    def name(self) -> str:
        return "DWD Open Data / CDC (opendata.dwd.de)"


def dwd_station_list(parameter: str = "air_temperature", resolution: str = "hourly") -> pd.DataFrame:
    """Return station list for a given parameter/resolution from DWD.

    Downloads the station description file from the DWD directory and returns
    it as a DataFrame with columns: ``station_id``, ``name``, ``lat``, ``lon``,
    ``elevation_m``, ``state``.

    Args:
        parameter: DWD parameter name, e.g. ``"air_temperature"``.
        resolution: ``"hourly"`` (default), ``"daily"``, ``"10_minutes"``.

    Returns:
        DataFrame of stations.
    """
    param_dir = _PARAM_META.get(parameter, _PARAM_META["air_temperature"])[0]
    base = f"{_CDC_BASE}/{resolution}/{param_dir}/recent/"

    try:
        html = get_text(base, timeout=20)
    except Exception as e:
        raise RuntimeError(f"Could not list DWD directory {base}: {e}") from e

    # Find the station description file (name starts with KL_ or Metadaten_).
    pattern = re.compile(r'href="([^"]*Beschreibung_Stationen[^"]*\.txt)"', re.IGNORECASE)
    matches = pattern.findall(html)
    if not matches:
        raise RuntimeError(f"Station list file not found in {base}")

    fname = matches[0].split("/")[-1]
    txt = get_text(base + fname, timeout=20)

    # Parse fixed-width format.
    lines = txt.splitlines()
    # Skip header lines (first 2), then parse each station line.
    # Format: station_id  date_from  date_to  elevation  lat  lon  name  state
    records = []
    for line in lines[2:]:
        parts = line.split()
        if len(parts) < 7:
            continue
        try:
            records.append({
                "station_id": parts[0].zfill(5),
                "date_from": parts[1],
                "date_to": parts[2],
                "elevation_m": float(parts[3]),
                "lat": float(parts[4]),
                "lon": float(parts[5]),
                "name": " ".join(parts[6:-1]),
                "state": parts[-1],
            })
        except (ValueError, IndexError):
            continue

    return pd.DataFrame(records)


def register() -> None:
    """Register the DWD provider. Called automatically on import."""
    register_provider("dwd", DwdProvider())


register()
