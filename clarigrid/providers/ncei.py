"""NOAA NCEI daily U.S. weather-station observations."""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator
from typing import Any, cast

import pandas as pd

from clarigrid.core.exceptions import ProviderError
from clarigrid.core.http import get_json
from clarigrid.core.interface import WeatherDataProvider
from clarigrid.core.normalise import EnergyFrame
from clarigrid.core.registry import register_provider
from clarigrid.utils.time import parse_dt

_URL = "https://www.ncei.noaa.gov/access/services/data/v1"
_DOCUMENTATION_URL = "https://www.ncei.noaa.gov/support/access-data-service-api-user-documentation"
_DATASET_URL = (
    "https://www.ncei.noaa.gov/products/land-based-station/"
    "global-historical-climatology-network-daily"
)
_LICENSE_URL = "https://www.noaa.gov/office-education/outreach-communication/faq"

_FIELDS = {
    "TMAX": ("temperature_max_c", "degC"),
    "TMIN": ("temperature_min_c", "degC"),
    "PRCP": ("precipitation_mm", "mm/day"),
    "AWND": ("wind_speed_ms", "m/s"),
    "SNOW": ("snowfall_mm", "mm/day"),
    "SNWD": ("snow_depth_mm", "mm"),
}
_DEFAULT_VARIABLES = tuple(_FIELDS)
_METADATA_FIELDS = {"STATION", "DATE", "NAME", "LATITUDE", "LONGITUDE", "ELEVATION"}


def _parse_zone(zone: str) -> str:
    """Validate a U.S. GHCN-Daily station identifier."""
    station = str(zone).strip().upper()
    if (
        len(station) != 11
        or not station.startswith("US")
        or not station.isascii()
        or not station.isalnum()
    ):
        raise ValueError(
            f"NCEI zone must be an 11-character U.S. GHCN station ID, got {zone!r}."
        )
    return station


def _resolve_variables(variables: Iterable[str] | None) -> list[str]:
    aliases = {target.lower(): source for source, (target, _) in _FIELDS.items()}
    requested = _DEFAULT_VARIABLES if variables is None else variables
    if isinstance(requested, str):
        requested = [requested]

    resolved: list[str] = []
    for variable in requested:
        value = str(variable).strip()
        source = aliases.get(value.lower(), value.upper())
        if source not in _FIELDS:
            raise ValueError(
                f"Unsupported NCEI variable {value!r}. "
                f"Choose from: {sorted(_FIELDS)} or {sorted(aliases)}."
            )
        if source not in resolved:
            resolved.append(source)
    if not resolved:
        raise ValueError("At least one NCEI variable must be requested.")
    return resolved


def _bounds(
    start: str | pd.Timestamp, end: str | pd.Timestamp
) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    start_utc = parse_dt(start)
    end_utc = parse_dt(end)
    if end_utc == end_utc.normalize():
        end_utc += pd.Timedelta(days=1)
    if end_utc <= start_utc:
        raise ValueError("NCEI end must be after start.")
    return start_utc, end_utc, start_utc.normalize(), (end_utc - pd.Timedelta("1ns")).normalize()


def _chunks(start: pd.Timestamp, end: pd.Timestamp) -> Iterator[tuple[pd.Timestamp, pd.Timestamp]]:
    # shortcut: bound responses to one calendar year; revisit if NCEI publishes a query limit.
    cursor = start
    while cursor <= end:
        year_end = pd.Timestamp(year=cursor.year, month=12, day=31, tz="UTC")
        chunk_end = min(year_end, end)
        yield cursor, chunk_end
        cursor = chunk_end + pd.Timedelta(days=1)


def _empty_frame(variables: list[str]) -> EnergyFrame:
    frame = EnergyFrame(  # type: ignore[no-untyped-call]
        columns=[_FIELDS[source][0] for source in variables], dtype=float
    )
    frame.index = pd.DatetimeIndex([], tz="UTC", name="utc_time")
    return frame


def _number(record: dict[str, Any], source: str) -> float | None:
    if source not in record:
        return None
    try:
        value = float(record[source])
    except (TypeError, ValueError) as exc:
        raise ProviderError(f"NCEI returned an invalid {source} value.") from exc
    if not math.isfinite(value):
        raise ProviderError(f"NCEI returned a non-finite {source} value.")
    return value


def _parse_response(data: Any, variables: list[str], station: str) -> EnergyFrame:
    """Parse NCEI daily-summaries JSON into canonical weather columns."""
    if not isinstance(data, list) or any(not isinstance(record, dict) for record in data):
        raise ProviderError("NCEI returned an unexpected daily-summaries JSON schema.")
    if not data:
        frame = _empty_frame(variables)
        station_name = latitude = longitude = elevation = None
    else:
        allowed = _METADATA_FIELDS | set(variables)
        timestamps: list[pd.Timestamp] = []
        rows: list[dict[str, float | None]] = []
        names: set[str] = set()
        locations: set[tuple[str | None, str | None, str | None]] = set()
        for record in data:
            if not {"STATION", "DATE"} <= record.keys() or set(record) - allowed:
                raise ProviderError("NCEI returned an unexpected daily-summaries JSON schema.")
            if record["STATION"] != station:
                raise ProviderError("NCEI returned observations for an unexpected station.")
            date = record["DATE"]
            if not isinstance(date, str) or len(date) != 10:
                raise ProviderError("NCEI returned an invalid observation date.")
            try:
                timestamp = pd.Timestamp(date).tz_localize("UTC")
            except (TypeError, ValueError) as exc:
                raise ProviderError("NCEI returned an invalid observation date.") from exc
            timestamps.append(timestamp)
            rows.append({_FIELDS[source][0]: _number(record, source) for source in variables})
            if isinstance(record.get("NAME"), str):
                names.add(record["NAME"])
            locations.add(
                (
                    record.get("LATITUDE"),
                    record.get("LONGITUDE"),
                    record.get("ELEVATION"),
                )
            )
        if len(names) > 1 or len(locations) > 1:
            raise ProviderError("NCEI returned inconsistent station metadata.")
        if len(set(timestamps)) != len(timestamps):
            raise ProviderError("NCEI returned duplicate daily observations.")
        frame = EnergyFrame(  # type: ignore[no-untyped-call]
            rows, index=pd.DatetimeIndex(timestamps, name="utc_time")
        ).sort_index()
        station_name = next(iter(names), None)
        raw_latitude, raw_longitude, raw_elevation = next(iter(locations))
        try:
            latitude = None if raw_latitude is None else float(raw_latitude)
            longitude = None if raw_longitude is None else float(raw_longitude)
            elevation = None if raw_elevation is None else float(raw_elevation)
        except (TypeError, ValueError) as exc:
            raise ProviderError("NCEI returned invalid station coordinates.") from exc

    frame._set_meta(
        provider="ncei",
        source_url=_DATASET_URL,
        documentation_url=_DOCUMENTATION_URL,
        license="U.S. public domain",
        license_url=_LICENSE_URL,
        attribution="NOAA National Centers for Environmental Information",
        temporal="daily station calendar date represented at 00:00 UTC",
        geographic_coverage="United States GHCN-Daily stations",
        temporal_coverage="station and variable dependent",
        station_id=station,
        station_name=station_name,
        latitude=latitude,
        longitude=longitude,
        elevation_m=elevation,
        units={_FIELDS[source][0]: _FIELDS[source][1] for source in variables},
    )
    frame.attrs.update(
        {
            "authentication": "none",
            "redistribution": "commercial and non-commercial; NOAA attribution requested",
            "api_version": "v1",
            "dataset_version": None,
            "api_stability": None,
            "pagination": None,
            "query_limit": None,
            "rate_limit": None,
            "update_frequency": "daily; full dataset rebuilt weekly",
            "resolution": "daily",
        }
    )
    return frame


class NCEIProvider(WeatherDataProvider):
    """Official NOAA NCEI daily observations for U.S. GHCN stations."""

    def get_weather(
        self,
        zone: str,
        start: str | pd.Timestamp,
        end: str | pd.Timestamp,
        *,
        variables: Iterable[str] | None = None,
        **kwargs: Any,
    ) -> pd.DataFrame:
        if kwargs:
            raise TypeError(f"Unsupported NCEI options: {', '.join(sorted(kwargs))}")
        station = _parse_zone(zone)
        requested = _resolve_variables(variables)
        start_utc, end_utc, query_start, query_end = _bounds(start, end)

        frames = []
        for chunk_start, chunk_end in _chunks(query_start, query_end):
            data = get_json(
                _URL,
                {
                    "dataset": "daily-summaries",
                    "stations": station,
                    "startDate": chunk_start.strftime("%Y-%m-%d"),
                    "endDate": chunk_end.strftime("%Y-%m-%d"),
                    "dataTypes": ",".join(requested),
                    "format": "json",
                    "units": "metric",
                    "includeAttributes": "false",
                    "includeStationName": "true",
                    "includeStationLocation": "true",
                },
            )
            frames.append(_parse_response(data, requested, station))

        metadata = dict(frames[0].attrs)
        frame = EnergyFrame(pd.concat(frames))  # type: ignore[no-untyped-call]
        frame.attrs.update(metadata)
        frame.sort_index(inplace=True)
        if frame.index.has_duplicates:
            raise ProviderError("NCEI returned duplicate daily observations across requests.")
        frame = cast(EnergyFrame, frame.loc[(frame.index >= start_utc) & (frame.index < end_utc)])
        frame.index.name = "utc_time"
        return frame

    def name(self) -> str:
        return "NOAA National Centers for Environmental Information"


def register() -> None:
    register_provider("ncei", NCEIProvider())


register()
