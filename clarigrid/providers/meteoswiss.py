"""MeteoSwiss hourly automatic weather-station observations."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from io import StringIO
from typing import Any, cast
from urllib.parse import urlparse

import pandas as pd

from clarigrid.core.exceptions import ProviderError
from clarigrid.core.http import get_bytes, get_json
from clarigrid.core.interface import WeatherDataProvider
from clarigrid.core.normalise import EnergyFrame
from clarigrid.core.registry import register_provider
from clarigrid.utils.time import parse_dt

_COLLECTION = "ch.meteoschweiz.ogd-smn"
_STAC_URL = f"https://data.geo.admin.ch/api/stac/v1/collections/{_COLLECTION}"
_PARAMETERS_URL = f"https://data.geo.admin.ch/{_COLLECTION}/ogd-smn_meta_parameters.csv"
_DOCUMENTATION_URL = (
    "https://opendatadocs.meteoswiss.ch/a-data-groundbased/"
    "a1-automatic-weather-stations"
)
_DOWNLOAD_URL = "https://opendatadocs.meteoswiss.ch/general/download"
_LICENSE_URL = "https://opendatadocs.meteoswiss.ch/general/terms-of-use"

_FIELDS = {
    "tre200h0": ("temperature_c", "°C", "degC"),
    "ure200h0": ("humidity_pct", "%", "%"),
    "pp0qnhh0": ("pressure_hpa", "hPa", "hPa"),
    "fkl010h0": ("wind_speed_ms", "m/s", "m/s"),
    "dkl010h0": ("wind_direction_deg", "°", "degree"),
    "fkl010h1": ("wind_gust_ms", "m/s", "m/s"),
    "rre150h0": ("precipitation_mm", "mm", "mm/1h"),
    "sre000h0": ("sunshine_duration_min", "min", "min/1h"),
}
_DEFAULT_VARIABLES = tuple(_FIELDS)
_HISTORICAL_ASSET = re.compile(r"^ogd-smn_[a-z]{3}_h_historical_(\d{4})-(\d{4})\.csv$")


def _parse_zone(zone: str) -> str:
    station = str(zone).strip().upper()
    if len(station) != 3 or not station.isascii() or not station.isalpha():
        raise ValueError(
            f"MeteoSwiss zone must be a three-letter SwissMetNet station code, got {zone!r}."
        )
    return station


def _resolve_variables(variables: Iterable[str] | None) -> list[str]:
    aliases = {target.lower(): source for source, (target, _, _) in _FIELDS.items()}
    requested = _DEFAULT_VARIABLES if variables is None else variables
    if isinstance(requested, str):
        requested = [requested]

    resolved: list[str] = []
    for variable in requested:
        value = str(variable).strip()
        source = aliases.get(value.lower(), value.lower())
        if source not in _FIELDS:
            raise ValueError(
                f"Unsupported MeteoSwiss variable {value!r}. "
                f"Choose from: {sorted(_FIELDS)} or {sorted(aliases)}."
            )
        if source not in resolved:
            resolved.append(source)
    if not resolved:
        raise ValueError("At least one MeteoSwiss variable must be requested.")
    return resolved


def _bounds(
    start: str | pd.Timestamp, end: str | pd.Timestamp
) -> tuple[pd.Timestamp, pd.Timestamp]:
    start_utc = parse_dt(start)
    end_utc = parse_dt(end)
    if end_utc == end_utc.normalize():
        end_utc += pd.Timedelta(days=1)
    if end_utc <= start_utc:
        raise ValueError("MeteoSwiss end must be after start.")
    return start_utc, end_utc


def _read_csv(payload: bytes, label: str) -> pd.DataFrame:
    try:
        text = payload.decode("cp1252")
        frame = pd.read_csv(StringIO(text), sep=";", dtype=str, keep_default_na=False)
    except (UnicodeDecodeError, pd.errors.ParserError) as exc:
        raise ProviderError(f"MeteoSwiss returned malformed {label} CSV.") from exc
    if frame.columns.has_duplicates:
        raise ProviderError(f"MeteoSwiss returned duplicate columns in {label} CSV.")
    return frame


def _parse_units(payload: bytes, variables: list[str]) -> None:
    metadata = _read_csv(payload, "parameter metadata")
    required = {"parameter_shortname", "parameter_granularity", "parameter_unit"}
    if not required <= set(metadata.columns):
        raise ProviderError("MeteoSwiss returned an unexpected parameter metadata schema.")

    for source in variables:
        rows = metadata.loc[metadata["parameter_shortname"] == source]
        expected_unit = _FIELDS[source][1]
        if (
            len(rows) != 1
            or rows.iloc[0]["parameter_granularity"] != "H"
            or rows.iloc[0]["parameter_unit"] != expected_unit
        ):
            actual = None if len(rows) != 1 else rows.iloc[0]["parameter_unit"]
            raise ProviderError(
                f"MeteoSwiss returned unexpected metadata for {source}: "
                f"unit {actual!r}; expected hourly {expected_unit!r}."
            )


def _parse_item(
    data: Any, station: str
) -> tuple[dict[str, Any], pd.Timestamp, str, float, float]:
    if (
        not isinstance(data, dict)
        or data.get("id") != station.lower()
        or data.get("collection") != _COLLECTION
        or data.get("type") != "Feature"
        or data.get("stac_version") != "1.0.0"
        or not isinstance(data.get("assets"), dict)
        or not isinstance(data.get("properties"), dict)
        or not isinstance(data.get("geometry"), dict)
    ):
        raise ProviderError("MeteoSwiss returned an unexpected STAC station schema.")

    properties = data["properties"]
    geometry = data["geometry"]
    title = properties.get("title")
    coordinates = geometry.get("coordinates")
    try:
        updated = pd.Timestamp(properties["updated"])
        if updated.tzinfo is None:
            raise ValueError
        updated = updated.tz_convert("UTC")
        if (
            geometry.get("type") != "Point"
            or not isinstance(coordinates, list)
            or len(coordinates) != 2
        ):
            raise ValueError
        longitude, latitude = (float(value) for value in coordinates)
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ProviderError("MeteoSwiss returned invalid station metadata.") from exc
    if not isinstance(title, str) or not title.endswith(f" ({station})"):
        raise ProviderError("MeteoSwiss returned an unexpected station title.")
    return data["assets"], updated, title[: -len(station) - 3], latitude, longitude


def _asset_url(asset: Any, station: str, name: str) -> str:
    expected_path = f"/{_COLLECTION}/{station.lower()}/{name}"
    if not isinstance(asset, dict) or asset.get("type") != "text/csv":
        raise ProviderError("MeteoSwiss returned an unexpected hourly asset schema.")
    url = asset.get("href")
    parsed = urlparse(url if isinstance(url, str) else "")
    if (
        parsed.scheme != "https"
        or parsed.netloc != "data.geo.admin.ch"
        or parsed.path != expected_path
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ProviderError("MeteoSwiss returned an unexpected hourly asset URL.")
    return cast(str, url)


def _select_assets(
    assets: dict[str, Any],
    station: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    updated: pd.Timestamp,
) -> list[tuple[str, str]]:
    prefix = f"ogd-smn_{station.lower()}_h_"
    current_year = updated.year
    current_year_start = pd.Timestamp(year=current_year, month=1, day=1, tz="UTC")
    selected: list[tuple[str, str]] = []

    for name, asset in assets.items():
        match = _HISTORICAL_ASSET.fullmatch(name)
        if not match or not name.startswith(prefix):
            continue
        asset_start = pd.Timestamp(year=int(match.group(1)), month=1, day=1, tz="UTC")
        asset_end = min(
            pd.Timestamp(year=int(match.group(2)) + 1, month=1, day=1, tz="UTC"),
            current_year_start,
        )
        if start < asset_end and end > asset_start:
            selected.append(("historical", _asset_url(asset, station, name)))

    today = updated.normalize()
    recent_name = f"{prefix}recent.csv"
    if start < today and end > current_year_start and recent_name in assets:
        selected.append(("recent", _asset_url(assets[recent_name], station, recent_name)))

    now_name = f"{prefix}now.csv"
    if start <= updated and end > today - pd.Timedelta(hours=12) and now_name in assets:
        selected.append(("now", _asset_url(assets[now_name], station, now_name)))

    if not selected and not any(name.startswith(prefix) for name in assets):
        raise ProviderError("MeteoSwiss station has no hourly observation assets.")
    return selected


def _empty_frame(variables: list[str]) -> EnergyFrame:
    frame = EnergyFrame(  # type: ignore[no-untyped-call]
        columns=[_FIELDS[source][0] for source in variables], dtype=float
    )
    frame.index = pd.DatetimeIndex([], tz="UTC", name="utc_time")
    return frame


def _parse_observations(payload: bytes, variables: list[str], station: str) -> EnergyFrame:
    raw = _read_csv(payload, "hourly observations")
    required = {"station_abbr", "reference_timestamp", *variables}
    if not required <= set(raw.columns):
        raise ProviderError("MeteoSwiss returned an unexpected hourly observation schema.")
    if not raw.empty and set(raw["station_abbr"]) != {station}:
        raise ProviderError("MeteoSwiss returned observations for an unexpected station.")

    try:
        timestamps = pd.to_datetime(
            raw["reference_timestamp"], format="%d.%m.%Y %H:%M", errors="raise", utc=True
        )
    except (TypeError, ValueError) as exc:
        raise ProviderError("MeteoSwiss returned an invalid observation timestamp.") from exc
    if timestamps.duplicated().any():
        raise ProviderError("MeteoSwiss returned duplicate hourly observations.")

    values: dict[str, Any] = {}
    for source in variables:
        try:
            numeric = pd.to_numeric(raw[source].replace("", pd.NA), errors="raise")
        except (TypeError, ValueError) as exc:
            raise ProviderError(f"MeteoSwiss returned an invalid {source} value.") from exc
        if not numeric.dropna().map(math.isfinite).all():
            raise ProviderError(f"MeteoSwiss returned a non-finite {source} value.")
        values[_FIELDS[source][0]] = numeric.to_numpy(dtype="float64", na_value=float("nan"))

    frame = EnergyFrame(  # type: ignore[no-untyped-call]
        values, index=pd.DatetimeIndex(timestamps, name="utc_time")
    )
    frame.sort_index(inplace=True)
    return frame


class MeteoSwissProvider(WeatherDataProvider):
    """Official hourly observations from SwissMetNet automatic stations."""

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
            raise TypeError(f"Unsupported MeteoSwiss options: {', '.join(sorted(kwargs))}")
        station = _parse_zone(zone)
        requested = _resolve_variables(variables)
        start_utc, end_utc = _bounds(start, end)

        item = get_json(f"{_STAC_URL}/items/{station.lower()}")
        assets, updated, station_name, latitude, longitude = _parse_item(item, station)
        _parse_units(get_bytes(_PARAMETERS_URL), requested)
        selected = _select_assets(assets, station, start_utc, end_utc, updated)

        frames = [_parse_observations(get_bytes(url), requested, station) for _, url in selected]
        if frames:
            frame = EnergyFrame(pd.concat(frames)).sort_index()  # type: ignore[no-untyped-call]
            if frame.index.has_duplicates:
                if "now" not in {kind for kind, _ in selected}:
                    raise ProviderError(
                        "MeteoSwiss returned duplicate observations across hourly files."
                    )
                frame = cast(EnergyFrame, frame.loc[~frame.index.duplicated(keep="last")])
            frame = cast(
                EnergyFrame, frame.loc[(frame.index >= start_utc) & (frame.index < end_utc)]
            )
        else:
            frame = _empty_frame(requested)
        frame.index.name = "utc_time"

        frame._set_meta(
            provider="meteoswiss",
            source_url=_DOCUMENTATION_URL,
            documentation_url=_DOWNLOAD_URL,
            license="CC BY 4.0",
            license_url=_LICENSE_URL,
            attribution="Source: MeteoSwiss",
            temporal="hourly station observation; timestamp is interval end",
            data_updated_at=updated.isoformat(),
            geographic_coverage="Switzerland SwissMetNet automatic stations",
            temporal_coverage="station and variable dependent",
            station_id=station,
            station_name=station_name,
            latitude=latitude,
            longitude=longitude,
            units={_FIELDS[source][0]: _FIELDS[source][2] for source in requested},
        )
        frame.attrs.update(
            {
                "authentication": "none",
                "redistribution": "commercial and non-commercial with attribution",
                "api_version": "OGC STAC API v1; STAC 1.0.0",
                "dataset_version": None,
                "api_stability": "v1 current; v0.9 deprecated",
                "pagination": "not applicable (specific station item)",
                "query_limit": None,
                "rate_limit": None,
                "update_frequency": (
                    "now file every 10 minutes; recent file daily; historical files annually"
                ),
                "resolution": "hourly",
            }
        )
        return frame

    def name(self) -> str:
        return "Federal Office of Meteorology and Climatology MeteoSwiss"


def register() -> None:
    register_provider("meteoswiss", MeteoSwissProvider())


register()
