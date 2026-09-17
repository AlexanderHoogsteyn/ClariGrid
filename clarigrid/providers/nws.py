"""U.S. National Weather Service hourly grid forecast provider.

The NWS API is unauthenticated but requires an identifying User-Agent. Point
locations use ``"lat,lon"`` and are resolved to the current NWS forecast grid
before quantitative forecast values are fetched and expanded to hourly rows.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pandas as pd

from clarigrid.core.exceptions import ProviderError
from clarigrid.core.http import get_json
from clarigrid.core.interface import WeatherDataProvider
from clarigrid.core.normalise import EnergyFrame
from clarigrid.core.registry import register_provider
from clarigrid.utils.time import parse_dt

_POINT_URL = "https://api.weather.gov/points/{latitude},{longitude}"
_DOCUMENTATION_URL = "https://www.weather.gov/documentation/services-web-api"
_LICENSE_URL = "https://www.weather.gov/disclaimer"
_HEADERS = {"Accept": "application/geo+json"}

_FIELDS = {
    "temperature": ("temperature_c", "wmoUnit:degC", 1.0),
    "dewpoint": ("dew_point_c", "wmoUnit:degC", 1.0),
    "relativeHumidity": ("humidity_pct", "wmoUnit:percent", 1.0),
    "probabilityOfPrecipitation": ("precipitation_probability_pct", "wmoUnit:percent", 1.0),
    "windSpeed": ("wind_speed_ms", "wmoUnit:km_h-1", 1 / 3.6),
    "windDirection": ("wind_direction_deg", "wmoUnit:degree_(angle)", 1.0),
    "skyCover": ("cloud_cover_pct", "wmoUnit:percent", 1.0),
}
_DEFAULT_VARIABLES = tuple(_FIELDS)
_UNITS = {
    "temperature_c": "degC",
    "dew_point_c": "degC",
    "humidity_pct": "%",
    "precipitation_probability_pct": "%",
    "wind_speed_ms": "m/s",
    "wind_direction_deg": "degree",
    "cloud_cover_pct": "%",
}


def _parse_zone(zone: str) -> tuple[float, float]:
    """Parse and validate an NWS ``"latitude,longitude"`` location."""
    parts = str(zone).split(",")
    if len(parts) != 2:
        raise ValueError(
            "NWS zone must be 'lat,lon' (e.g. '39.7456,-97.0892'), "
            f"got: {zone!r}"
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


def _resolve_variables(variables: Iterable[str] | None) -> list[str]:
    """Resolve NWS field names and canonical Clarigrid aliases."""
    aliases = {target.lower(): source for source, (target, _, _) in _FIELDS.items()}
    requested = _DEFAULT_VARIABLES if variables is None else variables
    if isinstance(requested, str):
        requested = [requested]

    resolved: list[str] = []
    for variable in requested:
        value = str(variable).strip()
        source = aliases.get(value.lower(), value)
        if source not in _FIELDS:
            raise ValueError(
                f"Unsupported NWS variable {value!r}. "
                f"Choose from: {sorted(_FIELDS)} or {sorted(aliases)}."
            )
        if source not in resolved:
            resolved.append(source)
    if not resolved:
        raise ValueError("At least one NWS variable must be requested.")
    return resolved


def _expand_values(values: list[dict[str, Any]], scale: float) -> pd.Series:
    """Expand NWS ISO-8601 valid-time intervals into hourly values."""
    expanded: dict[pd.Timestamp, float | None] = {}
    for item in values:
        valid_time = item.get("validTime", "")
        if "/" not in valid_time:
            continue
        start_text, duration_text = valid_time.split("/", 1)
        start = pd.Timestamp(start_text).tz_convert("UTC")
        duration = pd.Timedelta(duration_text)
        for timestamp in pd.date_range(start, start + duration, freq="h", inclusive="left"):
            value = item.get("value")
            expanded[timestamp] = None if value is None else float(value) * scale
    return pd.Series(expanded, dtype="float64")


def _parse_response(
    data: dict[str, Any],
    variables: list[str],
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    point: dict[str, Any] | None = None,
) -> EnergyFrame:
    """Convert NWS quantitative grid data to canonical hourly weather data."""
    properties = data.get("properties", {})
    columns: dict[str, pd.Series] = {}
    for source in variables:
        target, expected_unit, scale = _FIELDS[source]
        field = properties.get(source, {})
        unit = field.get("uom")
        if unit != expected_unit:
            raise ProviderError(
                f"NWS returned unexpected unit for {source}: {unit!r}; "
                f"expected {expected_unit!r}."
            )
        columns[target] = _expand_values(field.get("values", []), scale)

    frame = EnergyFrame(columns)  # type: ignore[no-untyped-call]
    index = pd.DatetimeIndex(frame.index)
    frame.index = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    frame.index.name = "utc_time"
    if not frame.empty:
        frame = frame.sort_index()
        start_ts = parse_dt(start)
        end_ts = parse_dt(end)
        if end_ts == end_ts.normalize():
            end_ts += pd.Timedelta(days=1)
        frame = frame.loc[(frame.index >= start_ts) & (frame.index < end_ts)]

    point_properties = (point or {}).get("properties", {})
    frame._set_meta(
        provider="nws",
        source_url=_DOCUMENTATION_URL,
        license="U.S. public domain unless otherwise noted",
        license_url=_LICENSE_URL,
        attribution="NOAA National Weather Service",
        temporal="hourly_forecast",
        update_time=properties.get("updateTime"),
        forecast_office=point_properties.get("gridId"),
        time_zone=point_properties.get("timeZone"),
        latitude=(point or {}).get("geometry", {}).get("coordinates", [None, None])[1],
        longitude=(point or {}).get("geometry", {}).get("coordinates", [None])[0],
        units={column: _UNITS[column] for column in frame.columns},
    )
    frame.attrs["rate_limit"] = None
    return frame


class NWSProvider(WeatherDataProvider):
    """Official NWS hourly forecast data for U.S. point locations."""

    def get_weather(
        self,
        zone: str,
        start: str | pd.Timestamp,
        end: str | pd.Timestamp,
        *,
        variables: Iterable[str] | None = None,
        **kwargs: Any,
    ) -> pd.DataFrame:
        """Return the currently available hourly forecast for a U.S. point."""
        if kwargs:
            raise TypeError(f"Unsupported NWS options: {', '.join(sorted(kwargs))}")
        latitude, longitude = _parse_zone(zone)
        requested = _resolve_variables(variables)
        point = get_json(
            _POINT_URL.format(latitude=latitude, longitude=longitude),
            headers=_HEADERS,
        )
        grid_url = point.get("properties", {}).get("forecastGridData")
        if not grid_url:
            raise ProviderError(f"NWS returned no forecast grid for {zone!r}.")
        forecast = get_json(grid_url, headers=_HEADERS)
        return _parse_response(forecast, requested, start, end, point=point)

    def name(self) -> str:
        return "NOAA National Weather Service"


def register() -> None:
    """Register the no-auth NWS provider."""
    register_provider("nws", NWSProvider())


register()
