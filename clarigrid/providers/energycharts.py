"""Fraunhofer ISE Energy-Charts provider.

The public API is documented at https://api.energy-charts.info/.  No API
key is required.  Values are converted to Clarigrid's canonical units and
column names at the provider boundary:

* power and capacity: MW
* stored energy capacity: MWh
* prices: EUR/MWh
* cross-border exchanges: signed MW (imports positive, exports negative)
* timestamps: timezone-aware UTC ``DatetimeIndex`` named ``utc_time``
"""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

from clarigrid.core.http import get_json
from clarigrid.core.interface import DataProvider
from clarigrid.core.registry import register_provider
from clarigrid.utils.time import normalise_index, parse_dt

_BASE = "https://api.energy-charts.info"
_SOURCE_URL = "https://www.energy-charts.info/"
_LICENSE = "CC BY 4.0"

# Price zones whose Energy-Charts documentation explicitly permits reuse.
_PRICE_ZONES = {
    "AT", "BE", "CH", "CZ", "DE_LU", "DE_AT_LU", "DK1", "DK2",
    "FR", "HU", "IT_NORD", "NL", "NO2", "PL", "SE4", "SI",
}

_BZN_BY_ZONE = {
    "DE_LU": "DE-LU",
    "DE_AT_LU": "DE-AT-LU",
    "IT_NORD": "IT-North",
}

# Energy-Charts power endpoints are country-based rather than bidding-zone
# based.  Bidding-zone subdivisions map to their containing country.
_COUNTRY_BY_ZONE = {
    **{code: code.lower() for code in {
        "AL", "AM", "AT", "AZ", "BA", "BE", "BG", "BY", "CH", "CY",
        "CZ", "EE", "ES", "FI", "FR", "GE", "GR", "HR", "HU", "IE",
        "LT", "LU", "LV", "MD", "ME", "MK", "MT", "NL", "PL", "PT",
        "RO", "RS", "SI", "SK", "TR", "UA", "XK",
    }},
    "DE_LU": "de",
    "DE_AT_LU": "de",
    "GB": "uk",
    "NI": "nie",
    "DK1": "dk",
    "DK2": "dk",
    "IT_NORD": "it",
    "IT": "it",
    "NO1": "no", "NO2": "no", "NO3": "no", "NO4": "no", "NO5": "no",
    "SE1": "se", "SE2": "se", "SE3": "se", "SE4": "se",
}

_POWER_NAME_MAP = {
    "Hydro Run-of-River": "hydro_run_of_river_mw",
    "Biomass": "biomass_mw",
    "Fossil brown coal / lignite": "lignite_mw",
    "Fossil hard coal": "hard_coal_mw",
    "Fossil oil": "oil_mw",
    "Fossil coal-derived gas": "coal_derived_gas_mw",
    "Fossil gas": "gas_mw",
    "Geothermal": "geothermal_mw",
    "Hydro water reservoir": "hydro_reservoir_mw",
    "Hydro pumped storage": "pumped_storage_mw",
    "Others": "other_mw",
    "Waste": "waste_mw",
    "Wind offshore": "wind_offshore_mw",
    "Wind onshore": "wind_onshore_mw",
    "Solar": "solar_mw",
}

_CAPACITY_NAME_MAP = {
    "Nuclear": "nuclear_capacity_mw",
    "Fossil brown coal / lignite": "lignite_capacity_mw",
    "Fossil hard coal": "hard_coal_capacity_mw",
    "Fossil gas": "gas_capacity_mw",
    "Fossil oil": "oil_capacity_mw",
    "Other, non-renewable": "other_nonrenewable_capacity_mw",
    "Hydro": "hydro_capacity_mw",
    "Hydro pumped storage": "pumped_storage_capacity_mw",
    "Battery storage (power)": "battery_storage_capacity_mw",
    "Battery storage (capacity)": "battery_storage_energy_mwh",
    "Biomass": "biomass_capacity_mw",
    "Wind offshore": "wind_offshore_capacity_mw",
    "Wind offshore planned (WindSeeG)": "wind_offshore_planned_capacity_mw",
    "Wind onshore": "wind_onshore_capacity_mw",
    "Wind onshore planned (EEG 2023)": "wind_onshore_planned_capacity_mw",
    "Solar DC": "solar_dc_capacity_mw",
    "Solar AC": "solar_ac_capacity_mw",
    "Solar planned (EEG 2023)": "solar_planned_capacity_mw",
}

_CONTINENTAL_FREQUENCY_ZONES = {
    "AT", "BE", "CH", "CZ", "DE_LU", "DE_AT_LU", "FR", "HU",
    "IT_NORD", "NL", "PL", "SI",
}


def _snake(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", value.lower())).strip("_")


def _api_params(zone: str, start: str | pd.Timestamp, end: str | pd.Timestamp) -> dict:
    return {
        "country": _country(zone),
        "start": str(start),
        "end": str(end),
    }


def _country(zone: str) -> str:
    try:
        return _COUNTRY_BY_ZONE[zone.upper()]
    except KeyError as exc:
        raise ValueError(f"Energy-Charts has no country mapping for zone {zone!r}.") from exc


def _bidding_zone(zone: str) -> str:
    upper = zone.upper()
    if upper not in _PRICE_ZONES:
        raise ValueError(
            f"Energy-Charts price data is not openly reusable for zone {zone!r}. "
            f"Supported zones: {sorted(_PRICE_ZONES)}"
        )
    return _BZN_BY_ZONE.get(upper, upper)


def _time_frame(unix_seconds: list[int] | None, columns: dict[str, list]) -> pd.DataFrame:
    timestamps = unix_seconds or []
    if not timestamps:
        return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name="utc_time"))

    index = pd.to_datetime(timestamps, unit="s", utc=True)
    values: dict[str, pd.Series] = {}
    for name, raw in columns.items():
        padded = list(raw or [])[:len(index)]
        padded.extend([None] * (len(index) - len(padded)))
        values[name] = pd.to_numeric(pd.Series(padded, index=index), errors="coerce")
    frame = pd.DataFrame(values, index=index)
    return normalise_index(frame.sort_index())


def _named_frame(
    payload: dict[str, Any],
    collection: str,
    names: dict[str, str] | None = None,
    *,
    multiplier: float = 1.0,
    prefix: str = "",
) -> pd.DataFrame:
    columns: dict[str, list] = {}
    for series in payload.get(collection) or []:
        source_name = series.get("name", "")
        if names is not None:
            canonical = names.get(source_name)
            if canonical is None:
                continue
        else:
            canonical = f"{prefix}{_snake(source_name)}_mw"
        raw = series.get("data") or []
        columns[canonical] = [value * multiplier if value is not None else None for value in raw]
    return _time_frame(payload.get("unix_seconds"), columns)


def _with_metadata(
    frame: pd.DataFrame,
    endpoint: str,
    *,
    unit: str | None = None,
    **metadata: Any,
) -> pd.DataFrame:
    frame.attrs.update({
        "source_url": f"{_BASE}/{endpoint}",
        "license": _LICENSE,
        **({"unit": unit} if unit else {}),
        **metadata,
    })
    return frame


class EnergyChartsProvider(DataProvider):
    """Fraunhofer ISE Energy-Charts public API."""

    def get_prices(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        payload = get_json(
            f"{_BASE}/price",
            {"bzn": _bidding_zone(zone), "start": str(start), "end": str(end)},
        )
        frame = _time_frame(payload.get("unix_seconds"), {
            "price_eur_mwh": payload.get("price") or [],
        })
        return _with_metadata(
            frame,
            "price",
            unit="EUR/MWh",
            currency="EUR",
            license_info=payload.get("license_info", ""),
        )

    def _public_power(self, zone: str, start: str, end: str) -> dict[str, Any]:
        return get_json(f"{_BASE}/public_power", _api_params(zone, start, end))

    def get_load(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        payload = self._public_power(zone, start, end)
        frame = _named_frame(payload, "production_types", {"Load": "load_mw"})
        return _with_metadata(frame, "public_power", unit="MW")

    def get_generation(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        payload = self._public_power(zone, start, end)
        frame = _named_frame(payload, "production_types", _POWER_NAME_MAP)
        return _with_metadata(frame, "public_power", unit="MW")

    def _forecast(
        self,
        zone: str,
        start: str,
        end: str,
        production_type: str,
        forecast_type: str = "day-ahead",
    ) -> pd.Series:
        payload = get_json(f"{_BASE}/public_power_forecast", {
            **_api_params(zone, start, end),
            "production_type": production_type,
            "forecast_type": forecast_type,
        })
        frame = _time_frame(payload.get("unix_seconds"), {
            production_type: payload.get("forecast_values") or [],
        })
        return frame[production_type] if production_type in frame else pd.Series(dtype=float)

    def get_generation_forecast(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        forecast_type = kwargs.get("forecast_type", "day-ahead")
        columns = {}
        for production_type in ("solar", "wind_onshore", "wind_offshore"):
            columns[f"{production_type}_forecast_mw"] = self._forecast(
                zone, start, end, production_type, forecast_type
            )
        frame = pd.concat(columns, axis=1) if columns else pd.DataFrame()
        frame.columns = list(columns)
        return _with_metadata(normalise_index(frame), "public_power_forecast", unit="MW")

    def get_load_forecast(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        series = self._forecast(zone, start, end, "load", "day-ahead")
        frame = normalise_index(series.rename("load_forecast_mw").to_frame())
        return _with_metadata(frame, "public_power_forecast", unit="MW")

    def _cross_border(
        self, endpoint: str, zone: str, start: str, end: str, prefix: str
    ) -> pd.DataFrame:
        payload = get_json(f"{_BASE}/{endpoint}", _api_params(zone, start, end))
        frame = _named_frame(
            payload, "countries", multiplier=1000.0, prefix=prefix
        )
        return _with_metadata(
            frame,
            endpoint,
            unit="MW",
            sign_convention="positive=import, negative=export",
        )

    def get_physical_flows(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        return self._cross_border("cbpf", zone, start, end, "flow_")

    def get_commercial_schedule(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        return self._cross_border("cbet", zone, start, end, "schedule_")

    def get_installed_capacity(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        time_step = kwargs.get("time_step", "yearly")
        installation_decommission = bool(kwargs.get("installation_decommission", False))
        payload = get_json(f"{_BASE}/installed_power", {
            "country": _country(zone),
            "time_step": time_step,
            "installation_decommission": installation_decommission,
        })
        raw_time = payload.get("time") or []
        index = pd.to_datetime(raw_time, utc=True, errors="coerce")
        columns: dict[str, list] = {}
        multiplier = 1.0 if installation_decommission else 1000.0
        for series in payload.get("production_types") or []:
            canonical = _CAPACITY_NAME_MAP.get(series.get("name", ""))
            if canonical is None:
                continue
            columns[canonical] = [
                value * multiplier if value is not None else None
                for value in (series.get("data") or [])
            ]
        frame = pd.DataFrame(columns, index=index)
        frame = frame[~frame.index.isna()]
        start_year, end_year = pd.Timestamp(start).year, pd.Timestamp(end).year
        frame = frame[(frame.index.year >= start_year) & (frame.index.year <= end_year)]
        frame = normalise_index(frame)
        return _with_metadata(
            frame,
            "installed_power",
            unit="MW; battery_storage_energy_mwh is MWh",
            last_update=pd.to_datetime(payload.get("last_update"), unit="s", utc=True),
        )

    def get_frequency(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        start_ts, end_ts = parse_dt(start), parse_dt(end)
        if end_ts - start_ts > pd.Timedelta(days=1):
            raise ValueError("Energy-Charts frequency requests are limited to 24 hours.")
        payload = get_json(f"{_BASE}/frequency", {
            "region": "DE-Freiburg",
            "start": start_ts.isoformat(),
            "end": end_ts.isoformat(),
        })
        frame = _time_frame(payload.get("unix_seconds"), {
            "frequency_hz": payload.get("data") or [],
        })
        return _with_metadata(frame, "frequency", unit="Hz", region="Continental Europe")

    def get_renewable_share(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        payload = self._public_power(zone, start, end)
        frame = _named_frame(payload, "production_types", {
            "Renewable share of load": "renewable_share_load_pct",
            "Renewable share of generation": "renewable_share_generation_pct",
        })
        return _with_metadata(frame, "public_power", unit="percent")

    def get_generation_share(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        generation = self.get_generation(zone, start, end)
        if generation.empty:
            return generation
        positive = generation.clip(lower=0)
        totals = positive.sum(axis=1)
        totals = totals.mask(totals.eq(0))
        frame = positive.div(totals, axis=0).mul(100)
        frame = frame.rename(columns={
            column: column.removesuffix("_mw") + "_share_pct"
            for column in frame.columns
        })
        return _with_metadata(frame, "public_power", unit="percent")

    def zones(self) -> set[str]:
        return set(_COUNTRY_BY_ZONE) | _PRICE_ZONES

    def capabilities(self) -> set[str]:
        return set(self.capability_zones())

    def capability_zones(self) -> dict[str, set[str]]:
        country_zones = set(_COUNTRY_BY_ZONE)
        return {
            "prices": set(_PRICE_ZONES),
            "load": country_zones,
            "generation": country_zones,
            "load_forecast": country_zones,
            "generation_forecast": country_zones,
            "physical_flows": country_zones,
            "commercial_schedule": country_zones,
            "installed_capacity": country_zones,
            "frequency": set(_CONTINENTAL_FREQUENCY_ZONES),
            "renewable_share": country_zones,
            "generation_share": country_zones,
        }

    def name(self) -> str:
        return "Energy-Charts (Fraunhofer ISE)"


def register() -> None:
    register_provider("energycharts", EnergyChartsProvider())


register()
