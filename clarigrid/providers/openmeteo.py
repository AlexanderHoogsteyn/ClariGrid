"""Open-Meteo provider — global weather forecast & ERA5 historical reanalysis.

No API key required for non-commercial use up to ~10,000 calls/day.
Self-registers on import as ``"openmeteo"``.

Usage::

    import clarigrid as cg
    cg.connect("openmeteo")

    # Forecast (next 7 days)
    df = cg.get_weather(
        "50.85,4.35",           # Brussels: "lat,lon"
        "2026-05-16",
        "2026-05-22",
        variables=["temperature_2m", "wind_speed_100m", "shortwave_radiation"],
    )

    # Historical ERA5 reanalysis
    df = cg.get_weather(
        "52.52,13.41",          # Berlin
        "2024-01-01",
        "2024-12-31",
        variables=["temperature_2m", "wind_speed_10m", "precipitation"],
        endpoint="archive",
    )

Zone format:
    ``"lat,lon"`` decimal degrees string, e.g. ``"50.85,4.35"``.
    Negative longitude for west (e.g. ``"-0.13,51.51"`` for London).

Endpoints:
    ``"auto"``    — default; uses forecast for future dates, archive for past.
    ``"forecast"`` — https://api.open-meteo.com/v1/forecast  (7–16 days ahead)
    ``"archive"``  — https://archive-api.open-meteo.com/v1/archive (ERA5, 1940+)
    ``"climate"``  — https://climate-api.open-meteo.com/v1/climate (CMIP6)
    ``"air_quality"`` — https://air-quality-api.open-meteo.com/v1/air-quality

Default variables (energy-relevant):
    ``temperature_2m`` (°C), ``wind_speed_100m`` (m/s),
    ``shortwave_radiation`` (W/m²)

All output is in UTC regardless of the location timezone.
"""

from __future__ import annotations

import pandas as pd

from clarigrid.core.http import get_json
from clarigrid.core.interface import WeatherDataProvider
from clarigrid.core.registry import register_provider
from clarigrid.core.types import STANDARD_TZ
from clarigrid.utils.time import normalise_index

_ENDPOINTS = {
    "forecast": "https://api.open-meteo.com/v1/forecast",
    "archive": "https://archive-api.open-meteo.com/v1/archive",
    "climate": "https://climate-api.open-meteo.com/v1/climate",
    "air_quality": "https://air-quality-api.open-meteo.com/v1/air-quality",
    "ensemble": "https://ensemble-api.open-meteo.com/v1/ensemble",
}

_DEFAULT_VARIABLES = ["temperature_2m", "wind_speed_100m", "shortwave_radiation"]

# Archive data is available up to approximately 5 days before today.
_ARCHIVE_LAG_DAYS = 6


def _choose_endpoint(start: str, end: str) -> str:
    """Pick forecast vs archive based on end date relative to today."""
    today = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None)
    cutoff = today - pd.Timedelta(days=_ARCHIVE_LAG_DAYS)
    end_ts = pd.Timestamp(end)
    return "archive" if end_ts <= cutoff else "forecast"


def _parse_zone(zone: str) -> tuple[float, float]:
    """Parse ``"lat,lon"`` string into (latitude, longitude) floats."""
    parts = zone.split(",")
    if len(parts) != 2:
        raise ValueError(
            f"Open-Meteo zone must be 'lat,lon' (e.g. '50.85,4.35'), got: {zone!r}"
        )
    try:
        return float(parts[0].strip()), float(parts[1].strip())
    except ValueError:
        raise ValueError(f"Could not parse lat/lon from zone: {zone!r}")


class OpenMeteoProvider(WeatherDataProvider):
    """Open-Meteo — global weather forecast and ERA5 historical reanalysis."""

    def get_weather(
        self,
        zone: str,
        start: str,
        end: str,
        *,
        variables: list[str] | None = None,
        endpoint: str = "auto",
        models: str | list[str] | None = None,
        **kwargs,
    ) -> pd.DataFrame:
        """Return hourly weather data as a UTC-indexed DataFrame.

        Args:
            zone: ``"lat,lon"`` string, e.g. ``"50.85,4.35"``.
            start: Start date (ISO format).
            end: End date (ISO format).
            variables: List of Open-Meteo hourly variable names. Defaults to
                ``["temperature_2m", "wind_speed_100m", "shortwave_radiation"]``.
            endpoint: API to use. ``"auto"`` selects forecast or archive based
                on the date range. See module docstring for options.
            models: Weather model override, e.g. ``"best_match"``, ``"era5"``,
                ``"ecmwf_ifs025"``.
            **kwargs: Extra params forwarded to the Open-Meteo API
                (e.g. ``wind_speed_unit="ms"``, ``forecast_days=7``).

        Returns:
            DataFrame with UTC ``DatetimeIndex`` and one column per variable.
        """
        lat, lon = _parse_zone(zone)
        vars_ = variables or _DEFAULT_VARIABLES

        if endpoint == "auto":
            endpoint = _choose_endpoint(start, end)
        url = _ENDPOINTS.get(endpoint, endpoint)  # allow raw URL too

        params: dict = {
            "latitude": lat,
            "longitude": lon,
            "hourly": ",".join(vars_),
            "start_date": pd.Timestamp(start).strftime("%Y-%m-%d"),
            "end_date": pd.Timestamp(end).strftime("%Y-%m-%d"),
            "timezone": "UTC",
            **kwargs,
        }
        if models:
            params["models"] = models if isinstance(models, str) else ",".join(models)

        data = get_json(url, params)
        return _parse_response(data, vars_)

    def name(self) -> str:
        return "Open-Meteo (open-meteo.com)"


def _parse_response(data: dict, variables: list[str]) -> pd.DataFrame:
    """Convert Open-Meteo JSON response to UTC-indexed DataFrame."""
    hourly = data.get("hourly")
    if not hourly:
        return pd.DataFrame()

    times_raw = hourly.get("time", [])
    if not times_raw:
        return pd.DataFrame()

    # Open-Meteo returns ISO strings like "2026-05-01T00:00".
    # When timezone=UTC is requested these are already UTC-naive ISO strings.
    times = pd.to_datetime(times_raw, utc=True)

    cols: dict[str, list] = {}
    for var in variables:
        vals = hourly.get(var)
        if vals is not None:
            cols[var] = vals

    if not cols:
        return pd.DataFrame()

    df = pd.DataFrame(cols, index=times)
    df.index.name = "utc_time"
    return normalise_index(df, STANDARD_TZ)


def register() -> None:
    """Register the Open-Meteo provider. Called automatically on import."""
    register_provider("openmeteo", OpenMeteoProvider())


register()
