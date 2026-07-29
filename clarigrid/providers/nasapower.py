"""NASA POWER global meteorology and solar-energy provider.

NASA POWER exposes analysis-ready MERRA-2 meteorology and CERES solar data
without authentication. Locations use the same ``"lat,lon"`` form as the
Open-Meteo provider.

The provider normalises source variables to Clarigrid's existing weather
columns and converts:

* surface pressure from kPa to hPa;
* daily solar irradiation from kWh/m2/day to mean W/m2;
* hourly solar irradiation from Wh/m2 to mean W/m2.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pandas as pd

from clarigrid.core.http import get_json
from clarigrid.core.interface import WeatherDataProvider
from clarigrid.core.registry import register_provider
from clarigrid.core.types import STANDARD_TZ
from clarigrid.utils.time import normalise_index

_BASE_URL = "https://power.larc.nasa.gov/api/temporal/{temporal}/point"
_DOCUMENTATION_URL = "https://power.larc.nasa.gov/docs/services/api/"
_LICENSE_URL = (
    "https://www.earthdata.nasa.gov/engage/open-data-services-software/"
    "data-use-policy"
)
_FILL_VALUE = -999.0
_MAX_PARAMETERS = {
    "daily": 20,
    "hourly": 15,
}

_COMMON_COLUMNS = {
    "T2M": "temperature_c",
    "T2MDEW": "dew_point_c",
    "RH2M": "humidity_pct",
    "WS10M": "wind_speed_ms",
    "WD10M": "wind_direction_deg",
    "PS": "pressure_hpa",
    "PRECTOTCORR": "precipitation_mm",
    "ALLSKY_SFC_SW_DWN": "shortwave_radiation_w_m2",
}
_DAILY_COLUMNS = {
    **_COMMON_COLUMNS,
    "T2M_MAX": "temperature_max_c",
    "T2M_MIN": "temperature_min_c",
}
_HOURLY_COLUMNS = dict(_COMMON_COLUMNS)

_DEFAULT_DAILY = [
    "T2M",
    "T2M_MAX",
    "T2M_MIN",
    "PRECTOTCORR",
    "WS10M",
    "RH2M",
    "ALLSKY_SFC_SW_DWN",
    "PS",
]
_DEFAULT_HOURLY = [
    "T2M",
    "T2MDEW",
    "PRECTOTCORR",
    "WS10M",
    "WD10M",
    "RH2M",
    "ALLSKY_SFC_SW_DWN",
    "PS",
]

_UNITS = {
    "temperature_c": "degC",
    "temperature_max_c": "degC",
    "temperature_min_c": "degC",
    "dew_point_c": "degC",
    "humidity_pct": "%",
    "wind_speed_ms": "m/s",
    "wind_direction_deg": "degree",
    "pressure_hpa": "hPa",
    "precipitation_mm": "mm/interval",
    "shortwave_radiation_w_m2": "W/m2",
}


def _parse_zone(zone: str) -> tuple[float, float]:
    """Parse and validate a ``"latitude,longitude"`` location."""
    parts = str(zone).split(",")
    if len(parts) != 2:
        raise ValueError(
            "NASA POWER zone must be 'lat,lon' "
            f"(e.g. '40.7128,-74.0060'), got: {zone!r}"
        )
    try:
        latitude, longitude = (float(part.strip()) for part in parts)
    except ValueError as exc:
        raise ValueError(f"Could not parse lat/lon from zone: {zone!r}") from exc

    if not -90 <= latitude <= 90:
        raise ValueError(f"Latitude must be between -90 and 90, got {latitude}.")
    if not -180 <= longitude <= 180:
        raise ValueError(f"Longitude must be between -180 and 180, got {longitude}.")
    return latitude, longitude


def _resolve_variables(
    variables: Iterable[str] | None,
    temporal: str,
) -> list[str]:
    """Resolve canonical column aliases and NASA parameter names."""
    columns = _DAILY_COLUMNS if temporal == "daily" else _HOURLY_COLUMNS
    defaults = _DEFAULT_DAILY if temporal == "daily" else _DEFAULT_HOURLY
    aliases = {column.lower(): parameter for parameter, column in columns.items()}

    requested = defaults if variables is None else variables
    if isinstance(requested, str):
        requested = [requested]

    resolved: list[str] = []
    for variable in requested:
        value = str(variable).strip()
        parameter = aliases.get(value.lower(), value.upper())
        if parameter not in resolved:
            resolved.append(parameter)

    if not resolved:
        raise ValueError("At least one NASA POWER variable must be requested.")
    max_parameters = _MAX_PARAMETERS[temporal]
    if len(resolved) > max_parameters:
        raise ValueError(
            f"NASA POWER accepts at most {max_parameters} parameters per "
            f"{temporal} request; got {len(resolved)}."
        )
    return resolved


def _parse_response(
    data: dict[str, Any],
    variables: list[str],
    temporal: str,
) -> pd.DataFrame:
    """Convert a NASA POWER point response to a canonical weather frame."""
    parameter_data = data.get("properties", {}).get("parameter", {})
    available = {name: parameter_data[name] for name in variables if name in parameter_data}
    if not available:
        return pd.DataFrame()

    timestamps = sorted({timestamp for values in available.values() for timestamp in values})
    time_format = "%Y%m%d" if temporal == "daily" else "%Y%m%d%H"
    index = pd.to_datetime(timestamps, format=time_format, utc=True)
    frame = pd.DataFrame(index=index)

    source_fill = data.get("header", {}).get("fill_value", _FILL_VALUE)
    columns = _DAILY_COLUMNS if temporal == "daily" else _HOURLY_COLUMNS
    for parameter, values in available.items():
        column = columns.get(parameter, parameter.lower())
        series = pd.to_numeric(
            pd.Series([values.get(timestamp) for timestamp in timestamps], index=index),
            errors="coerce",
        )
        series = series.mask(series.isin([source_fill, _FILL_VALUE]))

        if parameter == "PS":
            series = series * 10.0
        elif parameter == "ALLSKY_SFC_SW_DWN" and temporal == "daily":
            series = series * (1000.0 / 24.0)

        frame[column] = series.astype(float)

    frame = normalise_index(frame, STANDARD_TZ)
    coordinates = data.get("geometry", {}).get("coordinates", [])
    frame.attrs.update(
        {
            "provider": "nasapower",
            "source_url": _DOCUMENTATION_URL,
            "license": "CC0 unless NASA marks the source data otherwise",
            "license_url": _LICENSE_URL,
            "temporal": temporal,
            "time_standard": data.get("header", {}).get("time_standard", "UTC"),
            "latitude": coordinates[1] if len(coordinates) > 1 else None,
            "longitude": coordinates[0] if coordinates else None,
            "elevation_m": coordinates[2] if len(coordinates) > 2 else None,
            "units": {
                column: _UNITS[column]
                for column in frame.columns
                if column in _UNITS
            },
            "source_parameters": {
                columns.get(parameter, parameter.lower()): parameter
                for parameter in available
            },
        }
    )
    return frame


class NasaPowerProvider(WeatherDataProvider):
    """NASA POWER daily and hourly point meteorology and solar data."""

    def get_weather(
        self,
        zone: str,
        start: str | pd.Timestamp,
        end: str | pd.Timestamp,
        *,
        variables: Iterable[str] | None = None,
        temporal: str = "daily",
        community: str = "RE",
        **kwargs: Any,
    ) -> pd.DataFrame:
        """Return daily or hourly weather data for a latitude/longitude point.

        ``variables`` may contain NASA parameter codes such as ``"T2M"`` or
        canonical output names such as ``"temperature_c"``.
        """
        temporal = temporal.lower()
        if temporal not in {"daily", "hourly"}:
            raise ValueError("temporal must be either 'daily' or 'hourly'.")

        latitude, longitude = _parse_zone(zone)
        parameters = _resolve_variables(variables, temporal)
        params = {
            **kwargs,
            "parameters": ",".join(parameters),
            "community": community,
            "longitude": longitude,
            "latitude": latitude,
            "start": pd.Timestamp(start).strftime("%Y%m%d"),
            "end": pd.Timestamp(end).strftime("%Y%m%d"),
            "time-standard": "UTC",
            "format": "JSON",
        }
        data = get_json(_BASE_URL.format(temporal=temporal), params)
        return _parse_response(data, parameters, temporal)

    def name(self) -> str:
        return "NASA POWER"


def register() -> None:
    """Register the NASA POWER provider."""
    register_provider("nasapower", NasaPowerProvider())


register()
