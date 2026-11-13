"""Finnish Meteorological Institute hourly station observations."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree as ET

import pandas as pd

from clarigrid.core.exceptions import ProviderError
from clarigrid.core.http import get_text
from clarigrid.core.interface import WeatherDataProvider
from clarigrid.core.normalise import EnergyFrame
from clarigrid.core.registry import register_provider
from clarigrid.utils.time import parse_dt

_WFS_URL = "https://opendata.fmi.fi/wfs"
_META_URL = "https://opendata.fmi.fi/meta"
_DOCUMENTATION_URL = "https://en.ilmatieteenlaitos.fi/open-data-manual-time-series-data"
_LICENSE_URL = "https://ftp.csc.fi/pub/sci/geo/geodata/ilmatiede/FMI_terms_of_use_2018.pdf"
_STORED_QUERY = "fmi::observations::weather::timevaluepair"
_FMISID_CODESPACE = "http://xml.fmi.fi/namespace/stationcode/fmisid"
_LOCATION_NAME_CODESPACE = "http://xml.fmi.fi/namespace/locationcode/name"
_CHUNK_DAYS = 31

_FIELDS = {
    "t2m": ("temperature_c", "degC"),
    "td": ("dew_point_c", "degC"),
    "rh": ("humidity_pct", "%"),
    "p_sea": ("pressure_hpa", "hPa"),
    "ws_10min": ("wind_speed_ms", "m/s"),
    "wd_10min": ("wind_direction_deg", "deg"),
    "wg_10min": ("wind_gust_ms", "m/s"),
    "r_1h": ("precipitation_mm", "mm"),
}
_DEFAULT_VARIABLES = tuple(_FIELDS)
_OUTPUT_UNITS = {
    "temperature_c": "degC",
    "dew_point_c": "degC",
    "humidity_pct": "%",
    "pressure_hpa": "hPa",
    "wind_speed_ms": "m/s",
    "wind_direction_deg": "degree",
    "wind_gust_ms": "m/s",
    "precipitation_mm": "mm/1h",
}

_NS = {
    "wfs": "http://www.opengis.net/wfs/2.0",
    "om": "http://www.opengis.net/om/2.0",
    "gml": "http://www.opengis.net/gml/3.2",
    "wml2": "http://www.opengis.net/waterml/2.0",
}
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
_GML_ID = "{http://www.opengis.net/gml/3.2}id"
_OMOP = "http://inspire.ec.europa.eu/schemas/omop/2.9"


def _parse_zone(zone: str) -> str:
    """Validate an FMI station identifier."""
    station = str(zone).strip()
    if not station.isascii() or not station.isdigit():
        raise ValueError(f"FMI zone must be a numeric fmisid station code, got {zone!r}.")
    return station


def _resolve_variables(variables: Iterable[str] | None) -> list[str]:
    aliases = {target.lower(): source for source, (target, _) in _FIELDS.items()}
    requested = _DEFAULT_VARIABLES if variables is None else variables
    if isinstance(requested, str):
        requested = [requested]

    resolved: list[str] = []
    for variable in requested:
        value = str(variable).strip()
        source = aliases.get(value.lower(), value)
        if source not in _FIELDS:
            raise ValueError(
                f"Unsupported FMI variable {value!r}. "
                f"Choose from: {sorted(_FIELDS)} or {sorted(aliases)}."
            )
        if source not in resolved:
            resolved.append(source)
    if not resolved:
        raise ValueError("At least one FMI variable must be requested.")
    return resolved


def _bounds(
    start: str | pd.Timestamp, end: str | pd.Timestamp
) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    start_utc = pd.Timestamp(parse_dt(start))
    end_utc = pd.Timestamp(parse_dt(end))
    if end_utc == end_utc.normalize():
        end_utc += pd.Timedelta(days=1)
    if end_utc <= start_utc:
        raise ValueError("FMI end must be after start.")
    query_start = start_utc.floor("h")
    query_end = (end_utc - pd.Timedelta("1ns")).floor("h")
    return start_utc, end_utc, query_start, query_end


def _chunks(start: pd.Timestamp, end: pd.Timestamp) -> Iterator[tuple[pd.Timestamp, pd.Timestamp]]:
    # shortcut: bound XML responses to 31 days; revisit if FMI publishes a per-query limit.
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + pd.Timedelta(days=_CHUNK_DAYS) - pd.Timedelta(hours=1), end)
        yield cursor, chunk_end
        cursor = chunk_end + pd.Timedelta(hours=1)


def _parse_units(xml: str, variables: list[str]) -> None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ProviderError("FMI returned malformed parameter metadata XML.") from exc
    if root.tag != f"{{{_OMOP}}}CompositeObservableProperty":
        raise ProviderError("FMI returned an unexpected parameter metadata schema.")

    units: dict[str, str | None] = {}
    for item in root.findall(f".//{{{_OMOP}}}ObservableProperty"):
        source = item.get(_GML_ID)
        unit = item.find(f"{{{_OMOP}}}uom")
        if source:
            units[source] = None if unit is None else unit.get("uom")
    for source in variables:
        expected = _FIELDS[source][1]
        if units.get(source) != expected:
            raise ProviderError(
                f"FMI returned unexpected unit for {source}: {units.get(source)!r}; "
                f"expected {expected!r}."
            )


def _empty_frame(variables: list[str]) -> EnergyFrame:
    frame = EnergyFrame(  # type: ignore[no-untyped-call]
        columns=[_FIELDS[source][0] for source in variables], dtype=float
    )
    frame.index = pd.DatetimeIndex([], tz="UTC", name="utc_time")
    return frame


def _parse_response(xml: str, variables: list[str], station: str) -> EnergyFrame:
    """Parse FMI WaterML time/value members into canonical weather columns."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ProviderError("FMI returned malformed WFS XML.") from exc
    if root.tag != f"{{{_NS['wfs']}}}FeatureCollection":
        raise ProviderError("FMI returned an unexpected WFS response schema.")

    updated_at = root.get("timeStamp")
    if updated_at:
        try:
            pd.Timestamp(updated_at).tz_convert("UTC")
        except (TypeError, ValueError) as exc:
            raise ProviderError("FMI returned an invalid response timestamp.") from exc

    values: dict[str, dict[pd.Timestamp, float]] = {source: {} for source in variables}
    station_name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    for member in root.findall("wfs:member", _NS):
        observed = member.find(".//om:observedProperty", _NS)
        href = None if observed is None else observed.get(_XLINK_HREF)
        observed_url = urlparse(href or "")
        params = parse_qs(observed_url.query)
        source = params.get("param", [None])[0]
        if (
            observed_url.scheme != "https"
            or observed_url.netloc != "opendata.fmi.fi"
            or observed_url.path != "/meta"
            or params.get("observableProperty") != ["observation"]
            or source not in variables
        ):
            raise ProviderError("FMI returned an unexpected weather parameter.")

        identifiers = member.findall(".//gml:identifier", _NS)
        fmisids = [item.text for item in identifiers if item.get("codeSpace") == _FMISID_CODESPACE]
        if fmisids != [station]:
            raise ProviderError("FMI returned observations for an unexpected station.")

        names = member.findall(".//gml:name", _NS)
        station_names = [
            item.text
            for item in names
            if item.get("codeSpace") == _LOCATION_NAME_CODESPACE
        ]
        if station_names:
            station_name = station_names[0]
        position = member.find(".//gml:pos", _NS)
        if position is not None and position.text:
            try:
                latitude, longitude = (float(value) for value in position.text.split())
            except ValueError as exc:
                raise ProviderError("FMI returned invalid station coordinates.") from exc

        for point in member.findall(".//wml2:MeasurementTVP", _NS):
            time = point.findtext("wml2:time", namespaces=_NS)
            value = point.findtext("wml2:value", namespaces=_NS)
            try:
                if time is None or value is None:
                    raise ValueError
                timestamp = pd.Timestamp(time).tz_convert("UTC")
                numeric = float(value)
            except (TypeError, ValueError) as exc:
                raise ProviderError(
                    "FMI returned an invalid observation value or timestamp."
                ) from exc
            if timestamp in values[source]:
                raise ProviderError("FMI returned duplicate weather observations.")
            values[source][timestamp] = 0.0 if source == "r_1h" and numeric == -1 else numeric

    if not any(values.values()):
        frame = _empty_frame(variables)
    else:
        columns = {
            _FIELDS[source][0]: pd.Series(source_values, dtype="float64")
            for source, source_values in values.items()
        }
        frame = EnergyFrame(columns).sort_index()  # type: ignore[no-untyped-call]
        frame.index = pd.DatetimeIndex(frame.index, tz="UTC", name="utc_time")

    frame._set_meta(
        provider="fmi",
        source_url=_DOCUMENTATION_URL,
        license="CC BY 4.0",
        license_url=_LICENSE_URL,
        attribution="Finnish Meteorological Institute",
        temporal="hourly_station_observation",
        data_updated_at=updated_at,
        geographic_coverage="Finland observation stations",
        temporal_coverage="station and variable dependent",
        station_id=station,
        station_name=station_name,
        latitude=latitude,
        longitude=longitude,
        units={_FIELDS[source][0]: _OUTPUT_UNITS[_FIELDS[source][0]] for source in variables},
    )
    frame.attrs.update(
        {
            "authentication": "none",
            "redistribution": "commercial and non-commercial with attribution",
            "api_version": "OGC WFS 2.0.0",
            "dataset_version": None,
            "api_stability": None,
            "pagination": None,
            "query_limit": None,
            "rate_limit_requests_per_day": 20_000,
            "rate_limit_requests_per_5_minutes": 600,
            "update_frequency": "usually 10 minutes",
            "resolution": "hourly",
        }
    )
    return frame


class FMIProvider(WeatherDataProvider):
    """Official hourly weather observations from Finnish FMI stations."""

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
            raise TypeError(f"Unsupported FMI options: {', '.join(sorted(kwargs))}")
        station = _parse_zone(zone)
        requested = _resolve_variables(variables)
        start_utc, end_utc, query_start, query_end = _bounds(start, end)

        metadata = get_text(
            _META_URL,
            {
                "observableProperty": "observation",
                "param": ",".join(requested),
                "language": "eng",
            },
        )
        _parse_units(metadata, requested)

        frames = []
        for chunk_start, chunk_end in _chunks(query_start, query_end):
            xml = get_text(
                _WFS_URL,
                {
                    "service": "WFS",
                    "version": "2.0.0",
                    "request": "GetFeature",
                    "storedquery_id": _STORED_QUERY,
                    "fmisid": station,
                    "starttime": chunk_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "endtime": chunk_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "timestep": 60,
                    "parameters": ",".join(requested),
                },
            )
            frames.append(_parse_response(xml, requested, station))

        frame: EnergyFrame = EnergyFrame(pd.concat(frames))  # type: ignore[no-untyped-call]
        frame.sort_index(inplace=True)
        if frame.index.has_duplicates:
            raise ProviderError("FMI returned duplicate weather observations across requests.")
        frame = frame.loc[(frame.index >= start_utc) & (frame.index < end_utc)]
        frame.index.name = "utc_time"
        return frame

    def name(self) -> str:
        return "Finnish Meteorological Institute"


def register() -> None:
    register_provider("fmi", FMIProvider())


register()
