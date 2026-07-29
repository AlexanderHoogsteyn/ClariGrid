"""New York ISO public CSV archive provider.

NYISO publishes monthly ZIP archives containing one CSV per operating day.
The reports use local New York timestamps and different shapes for prices,
load, forecasts, and fuel mix. This provider selects only requested daily
members and converts every result to a UTC-indexed canonical frame.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from io import BytesIO
from typing import Any
from zipfile import BadZipFile, ZipFile

import pandas as pd

from clarigrid.core.exceptions import ProviderUnavailableError
from clarigrid.core.http import get_bytes
from clarigrid.core.interface import DataProvider
from clarigrid.core.registry import register_provider
from clarigrid.utils.time import normalise_index, parse_dt

_BASE = "https://mis.nyiso.com/public/csv"
_SOURCE_URL = "https://mis.nyiso.com/public/menu.htm"
_LICENSE = "NYISO public data terms and disclaimers"
_LOCAL_TZ = "America/New_York"

_REPORTS = {
    "prices": ("damlbmp", "damlbmp_zone"),
    "load": ("palIntegrated", "palIntegrated"),
    "generation": ("rtfuelmix", "rtfuelmix"),
    "load_forecast": ("isolf", "isolf"),
}

_LOAD_ZONES = {
    "NYISO_CAPITL": "CAPITL",
    "NYISO_CENTRL": "CENTRL",
    "NYISO_DUNWOD": "DUNWOD",
    "NYISO_GENESE": "GENESE",
    "NYISO_HUD_VL": "HUD VL",
    "NYISO_LONGIL": "LONGIL",
    "NYISO_MHK_VL": "MHK VL",
    "NYISO_MILLWD": "MILLWD",
    "NYISO_NYC": "N.Y.C.",
    "NYISO_NORTH": "NORTH",
    "NYISO_WEST": "WEST",
}

_PRICE_ZONES = dict(_LOAD_ZONES)

_FORECAST_COLUMNS = {
    "CAPITL": "Capitl",
    "CENTRL": "Centrl",
    "DUNWOD": "Dunwod",
    "GENESE": "Genese",
    "HUD VL": "Hud Vl",
    "LONGIL": "Longil",
    "MHK VL": "Mhk Vl",
    "MILLWD": "Millwd",
    "N.Y.C.": "N.Y.C.",
    "NORTH": "North",
    "WEST": "West",
}

_FUEL_COLUMNS = {
    "Dual Fuel": "dual_fuel_mw",
    "Hydro": "hydro_mw",
    "Natural Gas": "gas_mw",
    "Nuclear": "nuclear_mw",
    "Other Fossil Fuels": "other_fossil_mw",
    "Other Renewables": "other_renewable_mw",
    "Wind": "wind_mw",
}

_RENEWABLE_COLUMNS = {"hydro_mw", "other_renewable_mw", "wind_mw"}


def _local_date(value: str | pd.Timestamp) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert(_LOCAL_TZ).tz_localize(None)
    return timestamp.normalize()


def _month_starts(
    start: str | pd.Timestamp, end: str | pd.Timestamp
) -> Iterator[pd.Timestamp]:
    cursor = _local_date(start).replace(day=1)
    final = _local_date(end).replace(day=1)
    while cursor <= final:
        yield cursor
        cursor += pd.DateOffset(months=1)


def _archive_url(report: str, month: pd.Timestamp) -> str:
    directory, stem = _REPORTS[report]
    prefix = month.strftime("%Y%m01")
    return f"{_BASE}/{directory}/{prefix}{stem}_csv.zip"


def _read_archive(
    payload: bytes,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
) -> pd.DataFrame:
    """Read only CSV members whose date overlaps the requested local dates."""
    start_date = _local_date(start).date()
    end_date = _local_date(end).date()
    frames: list[pd.DataFrame] = []
    try:
        with ZipFile(BytesIO(payload)) as archive:
            for name in archive.namelist():
                match = re.match(r"(\d{8}).*\.csv$", name, flags=re.IGNORECASE)
                if not match:
                    continue
                member_date = pd.Timestamp(match.group(1)).date()
                if start_date <= member_date <= end_date:
                    frames.append(pd.read_csv(archive.open(name)))
    except BadZipFile as exc:
        preview = payload[:300].decode("utf-8", errors="replace")
        raise ProviderUnavailableError(
            f"NYISO returned an invalid ZIP response: {preview}"
        ) from exc
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _fetch_report(
    report: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
) -> pd.DataFrame:
    frames = [
        _read_archive(get_bytes(_archive_url(report, month), timeout=60), start, end)
        for month in _month_starts(start, end)
    ]
    frames = [frame for frame in frames if not frame.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _timestamps(frame: pd.DataFrame) -> pd.DatetimeIndex:
    """Parse NYISO local timestamps, using explicit EST/EDT when available."""
    naive = pd.to_datetime(frame["Time Stamp"], errors="coerce")
    if "Time Zone" in frame.columns:
        offsets = frame["Time Zone"].astype(str).str.upper().map(
            {"EST": "-05:00", "EDT": "-04:00"}
        )
        encoded = naive.dt.strftime("%Y-%m-%dT%H:%M:%S") + offsets.fillna("")
        return pd.DatetimeIndex(pd.to_datetime(encoded, utc=True, errors="coerce"))
    return pd.DatetimeIndex(
        naive.dt.tz_localize(
            _LOCAL_TZ, ambiguous=False, nonexistent="shift_forward"
        ).dt.tz_convert("UTC")
    )


def _filter_range(
    frame: pd.DataFrame,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
) -> pd.DataFrame:
    if frame.empty:
        return frame
    start_utc = parse_dt(start)
    end_utc = parse_dt(end)
    if end_utc <= start_utc:
        end_utc = start_utc + pd.Timedelta(days=1)
    return frame.loc[(frame.index >= start_utc) & (frame.index <= end_utc)]


def _metadata(frame: pd.DataFrame, report: str, **extra: Any) -> pd.DataFrame:
    directory, _stem = _REPORTS[report]
    frame.attrs.update({
        "source_url": f"{_BASE}/{directory}/",
        "documentation_url": _SOURCE_URL,
        "license": _LICENSE,
        **extra,
    })
    return frame


def _price_frame(raw: pd.DataFrame, location: str) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name="utc_time"))
    data = raw.loc[raw["Name"].astype(str) == location].copy()
    data.index = _timestamps(data)
    values = pd.to_numeric(data["LBMP ($/MWHr)"], errors="coerce")
    frame = values.groupby(level=0).last().to_frame("price_usd_mwh").sort_index()
    return normalise_index(frame)


def _load_frame(raw: pd.DataFrame, location: str | None) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name="utc_time"))
    data = raw.copy()
    data.index = _timestamps(data)
    data["value"] = pd.to_numeric(data["Integrated Load"], errors="coerce")
    if location is not None:
        data = data.loc[data["Name"].astype(str) == location]
        series = data["value"].groupby(level=0).last()
    else:
        series = data["value"].groupby(level=0).sum(min_count=1)
    return normalise_index(series.to_frame("load_mw").sort_index())


def _generation_frame(raw: pd.DataFrame) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name="utc_time"))
    data = raw.copy()
    data.index = _timestamps(data)
    data["column"] = data["Fuel Category"].map(_FUEL_COLUMNS)
    data["value"] = pd.to_numeric(data["Gen MW"], errors="coerce")
    data = data.dropna(subset=["column"])
    frame = data.pivot_table(
        index=data.index, columns="column", values="value", aggfunc="last"
    )
    frame.columns.name = None
    return normalise_index(frame.sort_index())


def _forecast_frame(raw: pd.DataFrame, location: str | None) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name="utc_time"))
    data = raw.copy()
    data.index = _timestamps(data)
    source_column = "NYISO" if location is None else _FORECAST_COLUMNS[location]
    values = pd.to_numeric(data[source_column], errors="coerce")
    frame = values.groupby(level=0).last().to_frame("load_forecast_mw").sort_index()
    return normalise_index(frame)


class NYISOProvider(DataProvider):
    """NYISO market and power-system reports without authentication."""

    def get_prices(
        self,
        zone: str,
        start: str,
        end: str,
        *,
        market: str = "day_ahead",
        node: str | None = None,
        **kwargs: Any,
    ) -> pd.DataFrame:
        if market != "day_ahead":
            raise ValueError("NYISO currently supports market='day_ahead' only.")
        location = node or _PRICE_ZONES.get(zone)
        if location is None:
            raise ValueError(
                "NYISO prices require a NYISO_* load-zone code or node='NYISO_NAME'."
            )
        frame = _filter_range(
            _price_frame(_fetch_report("prices", start, end), location), start, end
        )
        return _metadata(
            frame,
            "prices",
            currency="USD",
            unit="USD/MWh",
            market=market,
            location=location,
        )

    def get_load(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        location = _LOAD_ZONES.get(zone)
        frame = _filter_range(_load_frame(_fetch_report("load", start, end), location), start, end)
        return _metadata(frame, "load", unit="MW", location=location or "NYISO")

    def get_load_forecast(
        self, zone: str, start: str, end: str, **kwargs: Any
    ) -> pd.DataFrame:
        location = _LOAD_ZONES.get(zone)
        frame = _filter_range(
            _forecast_frame(_fetch_report("load_forecast", start, end), location),
            start,
            end,
        )
        return _metadata(
            frame, "load_forecast", unit="MW", location=location or "NYISO"
        )

    def get_generation(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        frame = _filter_range(
            _generation_frame(_fetch_report("generation", start, end)), start, end
        )
        return _metadata(frame, "generation", unit="MW", location="NYISO")

    def get_generation_share(
        self, zone: str, start: str, end: str, **kwargs: Any
    ) -> pd.DataFrame:
        generation = self.get_generation(zone, start, end)
        total = generation.sum(axis=1, min_count=1)
        shares = generation.div(total.where(total != 0), axis=0).mul(100.0)
        shares = shares.rename(columns={
            column: column.removesuffix("_mw") + "_share_pct"
            for column in shares.columns
        })
        return _metadata(shares, "generation", unit="percent", location="NYISO")

    def get_renewable_share(
        self, zone: str, start: str, end: str, **kwargs: Any
    ) -> pd.DataFrame:
        generation = self.get_generation(zone, start, end)
        renewable = generation.reindex(
            columns=sorted(_RENEWABLE_COLUMNS), fill_value=0.0
        ).sum(axis=1, min_count=1)
        total = generation.sum(axis=1, min_count=1)
        frame = (renewable / total.where(total != 0) * 100.0).to_frame(
            "renewable_share_generation_pct"
        )
        return _metadata(frame, "generation", unit="percent", location="NYISO")

    def capabilities(self) -> set[str]:
        return {
            "prices",
            "load",
            "load_forecast",
            "generation",
            "generation_share",
            "renewable_share",
        }

    def capability_zones(self) -> dict[str, set[str]]:
        market_zones = set(_LOAD_ZONES)
        return {
            "prices": set(_PRICE_ZONES),
            "load": {"NYIS", *market_zones},
            "load_forecast": {"NYIS", *market_zones},
            "generation": {"NYIS"},
            "generation_share": {"NYIS"},
            "renewable_share": {"NYIS"},
        }

    def zones(self) -> set[str]:
        return {"NYIS", *_LOAD_ZONES}

    def name(self) -> str:
        return "New York ISO Public Data"


register_provider("nyiso", NYISOProvider())
