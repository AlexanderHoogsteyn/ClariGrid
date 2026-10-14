"""MET Norway Locationforecast 2.0 global point forecast provider.

The public API requires no key, but every request must identify the client and
coordinates must use no more than four decimal places.  Clarigrid's shared HTTP
session supplies the identifying User-Agent; this provider rounds coordinates
before requesting the compact GeoJSON forecast.
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

_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
_DOCUMENTATION_URL = "https://api.met.no/weatherapi/locationforecast/2.0/documentation"
_LICENSE_URL = "https://api.met.no/doc/License"
_HEADERS = {"Accept": "application/geo+json"}

_FIELDS = {
    "air_temperature": ("temperature_c", "celsius", "instant"),
    "air_pressure_at_sea_level": ("pressure_hpa", "hPa", "instant"),
    "cloud_area_fraction": ("cloud_cover_pct", "%", "instant"),
    "relative_humidity": ("humidity_pct", "%", "instant"),
    "wind_from_direction": ("wind_direction_deg", "degrees", "instant"),
    "wind_speed": ("wind_speed_ms", "m/s", "instant"),
    "precipitation_amount": ("precipitation_mm", "mm", "next_1_hours"),
}
_DEFAULT_VARIABLES = tuple(_FIELDS)
_OUTPUT_UNITS = {
    "temperature_c": "degC",
    "pressure_hpa": "hPa",
    "cloud_cover_pct": "%",
    "humidity_pct": "%",
    "wind_direction_deg": "degree",
    "wind_speed_ms": "m/s",
    "precipitation_mm": "mm/1h",
}


def _parse_zone(zone: str) -> tuple[float, float]:
    """Parse, validate, and round a ``"latitude,longitude"`` location."""
    parts = str(zone).split(",")
    if len(parts) != 2:
        raise ValueError(
            "MET Norway zone must be 'lat,lon' (e.g. '59.9139,10.7522'), "
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
    return round(latitude, 4), round(longitude, 4)


def _resolve_variables(variables: Iterable[str] | None) -> list[str]:
    """Resolve upstream names and Clarigrid canonical weather aliases."""
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
                f"Unsupported MET Norway variable {value!r}. "
                f"Choose from: {sorted(_FIELDS)} or {sorted(aliases)}."
            )
        if source not in resolved:
            resolved.append(source)
    if not resolved:
        raise ValueError("At least one MET Norway variable must be requested.")
    return resolved


def _empty_frame(variables: list[str]) -> EnergyFrame:
    frame = EnergyFrame(  # type: ignore[no-untyped-call]
        columns=[_FIELDS[source][0] for source in variables], dtype=float
    )
    frame.index = pd.DatetimeIndex([], tz="UTC", name="utc_time")
    return frame


def _parse_response(
    data: dict[str, Any],
    variables: list[str],
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
) -> EnergyFrame:
    """Convert compact METJSON into canonical UTC point-forecast data."""
    if data.get("type") != "Feature" or not isinstance(data.get("properties"), dict):
        raise ProviderError("MET Norway returned an unexpected GeoJSON response schema.")

    properties = data["properties"]
    meta = properties.get("meta")
    timeseries = properties.get("timeseries")
    if not isinstance(meta, dict) or not isinstance(meta.get("units"), dict):
        raise ProviderError("MET Norway response is missing forecast unit metadata.")
    if not isinstance(timeseries, list):
        raise ProviderError("MET Norway response is missing the forecast timeseries.")

    source_units = meta["units"]
    for source in variables:
        expected = _FIELDS[source][1]
        actual = source_units.get(source)
        if actual != expected:
            raise ProviderError(
                f"MET Norway returned unexpected unit for {source}: {actual!r}; "
                f"expected {expected!r}."
            )

    timestamps: list[str] = []
    rows: list[dict[str, float | None]] = []
    for item in timeseries:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("time"), str)
        ):
            raise ProviderError("MET Norway returned a malformed forecast time step.")
        payload = item.get("data")
        if not isinstance(payload, dict):
            raise ProviderError("MET Norway returned a forecast step without data.")
        timestamps.append(item["time"])
        row: dict[str, float | None] = {}
        for source in variables:
            target, _, section = _FIELDS[source]
            details = payload.get(section, {})
            if isinstance(details, dict):
                details = details.get("details", {})
            value = details.get(source) if isinstance(details, dict) else None
            row[target] = value
        rows.append(row)

    if not rows:
        frame = _empty_frame(variables)
    else:
        try:
            index = pd.to_datetime(timestamps, utc=True)
        except (TypeError, ValueError) as exc:
            raise ProviderError("MET Norway returned an invalid forecast timestamp.") from exc
        frame = EnergyFrame(  # type: ignore[no-untyped-call]
            rows, index=pd.DatetimeIndex(index, name="utc_time")
        )
        for column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.sort_index()

        start_ts = parse_dt(start)
        end_ts = parse_dt(end)
        if end_ts == end_ts.normalize():
            end_ts += pd.Timedelta(days=1)
        frame = frame.loc[(frame.index >= start_ts) & (frame.index < end_ts)]

    coordinates = data.get("geometry", {}).get("coordinates", [])
    frame._set_meta(
        provider="metno",
        source_url=_DOCUMENTATION_URL,
        license="NLOD 2.0 / CC BY 4.0",
        license_url=_LICENSE_URL,
        attribution="Data from MET Norway",
        temporal="point_forecast",
        data_updated_at=meta.get("updated_at"),
        coverage="global",
        latitude=coordinates[1] if len(coordinates) > 1 else None,
        longitude=coordinates[0] if coordinates else None,
        elevation_m=coordinates[2] if len(coordinates) > 2 else None,
        units={column: _OUTPUT_UNITS[column] for column in frame.columns},
    )
    frame.attrs.update(
        {
            "pagination": None,
            "query_limit": None,
            "rate_limit_requests_per_second": 20,
        }
    )
    return frame


class MetNoProvider(WeatherDataProvider):
    """Official MET Norway compact forecasts for global point locations."""

    def get_weather(
        self,
        zone: str,
        start: str | pd.Timestamp,
        end: str | pd.Timestamp,
        *,
        variables: Iterable[str] | None = None,
        altitude: int | None = None,
        **kwargs: Any,
    ) -> pd.DataFrame:
        """Return the currently available point forecast for ``zone``."""
        if kwargs:
            raise TypeError(f"Unsupported MET Norway options: {', '.join(sorted(kwargs))}")
        latitude, longitude = _parse_zone(zone)
        requested = _resolve_variables(variables)
        params: dict[str, float | int] = {"lat": latitude, "lon": longitude}
        if altitude is not None:
            if isinstance(altitude, bool) or not isinstance(altitude, int):
                raise ValueError("altitude must be a whole number of metres.")
            params["altitude"] = altitude
        data = get_json(_URL, params=params, headers=_HEADERS)
        if not isinstance(data, dict):
            raise ProviderError("MET Norway returned a non-object JSON response.")
        return _parse_response(data, requested, start, end)

    def name(self) -> str:
        return "MET Norway Locationforecast"


def register() -> None:
    """Register the no-key MET Norway provider."""
    register_provider("metno", MetNoProvider())


register()
