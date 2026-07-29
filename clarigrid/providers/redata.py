"""Red Electrica de Espana (REData) open-data provider.

REData exposes public JSONAPI widgets without authentication.  The widgets
do not share one schema: real-time demand is a flat five-minute MW series,
while generation and cross-border widgets contain nested daily MWh series.
This provider normalises both shapes at the provider boundary.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterator

import pandas as pd

from clarigrid.core.exceptions import ProviderUnavailableError
from clarigrid.core.http import get_json
from clarigrid.core.interface import DataProvider
from clarigrid.core.registry import register_provider
from clarigrid.utils.time import normalise_index

_BASE = "https://apidatos.ree.es/es/datos"
_SOURCE_URL = "https://www.ree.es/en/datos/apidata"
_LICENSE = "REData terms of use"
_LOCAL_TZ = "Europe/Madrid"
_PENINSULAR_GEO = {
    "geo_trunc": "electric_system",
    "geo_limit": "peninsular",
    "geo_ids": 8741,
}

_GENERATION_NAMES = {
    "hidraulica": "hydro_mw",
    "eolica": "wind_onshore_mw",
    "solar fotovoltaica": "solar_mw",
    "solar termica": "solar_thermal_mw",
    "hidroeolica": "hydro_wind_mw",
    "otras renovables": "other_renewable_mw",
    "residuos renovables": "renewable_waste_mw",
    "nuclear": "nuclear_mw",
    "ciclo combinado": "combined_cycle_mw",
    "carbon": "hard_coal_mw",
    "fuel gas": "oil_gas_mw",
    "motores diesel": "diesel_mw",
    "turbina de gas": "gas_turbine_mw",
    "turbina de vapor": "steam_turbine_mw",
    "cogeneracion": "cogeneration_mw",
    "residuos no renovables": "nonrenewable_waste_mw",
    "turbinacion bombeo": "pumped_storage_mw",
    "entrega bateria": "battery_discharge_mw",
}

_CAPACITY_NAMES = {
    key: value.removesuffix("_mw") + "_capacity_mw"
    for key, value in _GENERATION_NAMES.items()
    if key not in {"turbinacion bombeo", "entrega bateria"}
}

_REALTIME_NAMES = {
    "real": "load_mw",
    "demanda real": "load_mw",
    "forecasted": "load_forecast_mw",
    "prevista": "load_forecast_mw",
    "demanda prevista": "load_forecast_mw",
    "scheduled": "load_scheduled_mw",
    "programada": "load_scheduled_mw",
    "demanda programada": "load_scheduled_mw",
}

_BORDER_NAMES = {
    "francia": "fr",
    "france": "fr",
    "portugal": "pt",
    "marruecos": "ma",
    "morocco": "ma",
    "andorra": "ad",
}


def _key(value: Any) -> str:
    """Return an accent-insensitive key for multilingual REData labels."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name="utc_time"))


def _query_timestamp(value: str | pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert(_LOCAL_TZ).tz_localize(None)
    return ts


def _format_timestamp(value: pd.Timestamp) -> str:
    return value.strftime("%Y-%m-%dT%H:%M")


def _chunks(
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    max_days: int,
) -> Iterator[tuple[pd.Timestamp, pd.Timestamp]]:
    """Yield overlapping bounded requests; duplicate boundaries are removed later."""
    cursor = _query_timestamp(start)
    final = _query_timestamp(end)
    while cursor <= final:
        chunk_end = min(cursor + pd.Timedelta(days=max_days), final)
        yield cursor, chunk_end
        if chunk_end == final:
            break
        cursor = chunk_end


def _fetch_payloads(
    category: str,
    widget: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    time_trunc: str,
    max_days: int,
    peninsular: bool = False,
) -> list[dict[str, Any]]:
    payloads = []
    url = f"{_BASE}/{category}/{widget}"
    for chunk_start, chunk_end in _chunks(start, end, max_days):
        params: dict[str, Any] = {
            "start_date": _format_timestamp(chunk_start),
            "end_date": _format_timestamp(chunk_end),
            "time_trunc": time_trunc,
        }
        if peninsular:
            params.update(_PENINSULAR_GEO)
        payload = get_json(url, params)
        if payload.get("errors"):
            detail = payload["errors"][0].get("detail", "unknown REData error")
            raise ProviderUnavailableError(f"REData rejected {category}/{widget}: {detail}")
        payloads.append(payload)
    return payloads


def _iter_indicators(payload: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Flatten direct and nested JSONAPI indicators with their parent title."""
    for item in payload.get("included") or []:
        attributes = item.get("attributes") or {}
        parent = str(attributes.get("title") or item.get("type") or "")
        content = attributes.get("content")
        if isinstance(content, list):
            for child in content:
                yield parent, child.get("attributes") or {}
        else:
            yield "", attributes


def _parse_timestamp(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize(_LOCAL_TZ)
    return ts.tz_convert("UTC")


def _period_hours(value: Any, period: str) -> float:
    """Return actual elapsed hours, including 23/25-hour Spanish DST days."""
    local = _parse_timestamp(value).tz_convert(_LOCAL_TZ)
    if period == "hour":
        return 1.0
    if period == "day":
        start = local.normalize()
        end = start + pd.DateOffset(days=1)
    elif period == "month":
        start = local.normalize().replace(day=1)
        end = start + pd.DateOffset(months=1)
    elif period == "year":
        start = local.normalize().replace(month=1, day=1)
        end = start + pd.DateOffset(years=1)
    else:
        raise ValueError(f"Unsupported REData energy period: {period!r}")
    return (end.tz_convert("UTC") - start.tz_convert("UTC")).total_seconds() / 3600


def _series(attributes: dict[str, Any], *, energy_period: str | None) -> pd.Series:
    rows = attributes.get("values") or []
    if not rows:
        return pd.Series(dtype=float)
    index = [_parse_timestamp(row.get("datetime")) for row in rows]
    values = [pd.to_numeric(row.get("value"), errors="coerce") for row in rows]
    if energy_period is not None:
        values = [
            value / _period_hours(row.get("datetime"), energy_period)
            if pd.notna(value) else value
            for value, row in zip(values, rows)
        ]
    result = pd.Series(values, index=pd.DatetimeIndex(index), dtype=float)
    return result.groupby(level=0).sum(min_count=1).sort_index()


def _indicator_frame(
    payload: dict[str, Any],
    names: dict[str, str],
    *,
    energy_period: str | None = None,
) -> pd.DataFrame:
    columns: dict[str, pd.Series] = {}
    for _parent, attributes in _iter_indicators(payload):
        canonical = names.get(_key(attributes.get("title")))
        if canonical is None or attributes.get("composite") is True:
            continue
        values = _series(attributes, energy_period=energy_period)
        if canonical in columns:
            columns[canonical] = columns[canonical].add(values, fill_value=0)
        else:
            columns[canonical] = values
    if not columns:
        return _empty_frame()
    return normalise_index(pd.concat(columns, axis=1).sort_index())


def _flow_frame(payload: dict[str, Any]) -> pd.DataFrame:
    """Return one signed net-flow column per border (import positive)."""
    columns: dict[str, pd.Series] = {}
    for parent, attributes in _iter_indicators(payload):
        border = _BORDER_NAMES.get(_key(parent))
        title = _key(attributes.get("title"))
        if border is None or title not in {"saldo", "balance"}:
            continue
        columns[f"flow_{border}_mw"] = _series(attributes, energy_period="day")
    if not columns:
        return _empty_frame()
    return normalise_index(pd.concat(columns, axis=1).sort_index())


def _concat_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return _empty_frame()
    frame = pd.concat(frames).sort_index()
    frame = frame.groupby(level=0).first()
    return normalise_index(frame)


def _metadata(
    frame: pd.DataFrame,
    endpoint: str,
    *,
    unit: str,
    resolution: str,
    **metadata: Any,
) -> pd.DataFrame:
    frame.attrs.update({
        "source_url": f"{_BASE}/{endpoint}",
        "documentation_url": _SOURCE_URL,
        "license": _LICENSE,
        "unit": unit,
        "resolution": resolution,
        "area": "ES peninsular bidding zone",
        **metadata,
    })
    return frame


class REDataProvider(DataProvider):
    """Spanish electricity-system data from Red Electrica's open API."""

    def _realtime_demand(self, start: str, end: str) -> pd.DataFrame:
        frames = [
            _indicator_frame(payload, _REALTIME_NAMES)
            for payload in _fetch_payloads(
                "demanda", "demanda-tiempo-real", start, end,
                time_trunc="hour", max_days=30,
            )
        ]
        return _concat_frames(frames)

    def get_prices(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        raise NotImplementedError(
            "REData's no-auth market widget is not a canonical day-ahead price "
            "source. Connect 'energycharts' or 'entsoe' for ES prices."
        )

    def get_load(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        frame = self._realtime_demand(start, end).reindex(columns=["load_mw"])
        return _metadata(
            frame, "demanda/demanda-tiempo-real",
            unit="MW", resolution="5 minutes", data_type="actual",
        )

    def get_load_forecast(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        frame = self._realtime_demand(start, end).reindex(columns=["load_forecast_mw"])
        return _metadata(
            frame, "demanda/demanda-tiempo-real",
            unit="MW", resolution="5 minutes", data_type="forecast",
        )

    def get_generation(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        frames = [
            _indicator_frame(payload, _GENERATION_NAMES, energy_period="day")
            for payload in _fetch_payloads(
                "balance", "balance-electrico", start, end,
                time_trunc="day", max_days=365, peninsular=True,
            )
        ]
        frame = _concat_frames(frames)
        return _metadata(
            frame, "balance/balance-electrico",
            unit="MW", resolution="daily average", source_unit="MWh",
            data_type="historical",
        )

    def get_generation_share(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        generation = self.get_generation(zone, start, end)
        if generation.empty:
            return generation
        positive = generation.clip(lower=0)
        totals = positive.sum(axis=1).mask(lambda values: values.eq(0))
        frame = positive.div(totals, axis=0).mul(100).rename(columns={
            column: column.removesuffix("_mw") + "_share_pct"
            for column in positive.columns
        })
        return _metadata(
            frame, "balance/balance-electrico",
            unit="percent", resolution="daily", data_type="historical",
        )

    def get_renewable_share(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        generation = self.get_generation(zone, start, end)
        if generation.empty:
            return generation
        renewable = [
            column for column in generation
            if column in {
                "hydro_mw", "wind_onshore_mw", "solar_mw", "solar_thermal_mw",
                "hydro_wind_mw", "other_renewable_mw", "renewable_waste_mw",
            }
        ]
        positive = generation.clip(lower=0)
        total = positive.sum(axis=1).mask(lambda values: values.eq(0))
        frame = positive[renewable].sum(axis=1).div(total).mul(100).to_frame(
            "renewable_share_generation_pct"
        )
        return _metadata(
            frame, "balance/balance-electrico",
            unit="percent", resolution="daily", data_type="historical",
        )

    def get_physical_flows(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        frames = [
            _flow_frame(payload)
            for payload in _fetch_payloads(
                "intercambios", "todas-fronteras-fisicos", start, end,
                time_trunc="day", max_days=365,
            )
        ]
        frame = _concat_frames(frames)
        return _metadata(
            frame, "intercambios/todas-fronteras-fisicos",
            unit="MW", resolution="daily average", source_unit="MWh",
            sign_convention="positive=import, negative=export",
            data_type="historical",
        )

    def get_installed_capacity(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        time_step = kwargs.get("time_step", "yearly")
        time_trunc = {
            "monthly": "month",
            "month": "month",
            "yearly": "year",
            "year": "year",
        }.get(time_step)
        if time_trunc is None:
            raise ValueError("REData time_step must be 'monthly' or 'yearly'.")
        frames = [
            _indicator_frame(payload, _CAPACITY_NAMES)
            for payload in _fetch_payloads(
                "generacion", "potencia-instalada-generacion", start, end,
                time_trunc=time_trunc, max_days=700, peninsular=True,
            )
        ]
        frame = _concat_frames(frames)
        return _metadata(
            frame, "generacion/potencia-instalada-generacion",
            unit="MW", resolution=time_trunc, data_type="historical",
        )

    def zones(self) -> set[str]:
        return {"ES"}

    def capabilities(self) -> set[str]:
        return {
            "load", "load_forecast", "generation", "generation_share",
            "renewable_share", "physical_flows", "installed_capacity",
        }

    def name(self) -> str:
        return "REData (Red Electrica de Espana)"


def register() -> None:
    register_provider("redata", REDataProvider())


register()
