"""U.S. Energy Information Administration EIA-930 provider.

The EIA API v2 exposes hourly balancing-authority demand, forecasts,
generation by fuel, and physical interchange. A free API key is required.
Configure it with ``clarigrid keys set eia YOUR_KEY`` or the
``CLARIGRID_EIA_API_KEY`` environment variable.

EIA labels hourly observations as MWh. Because each observation covers one
hour, those values are numerically equal to average MW. This provider makes
that interval conversion explicit at the provider boundary.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from clarigrid.core import config
from clarigrid.core.http import get_json
from clarigrid.core.interface import DataProvider
from clarigrid.core.registry import register_provider
from clarigrid.core.types import COLUMN_LOAD
from clarigrid.utils.time import normalise_index, parse_dt

_BASE = "https://api.eia.gov/v2/electricity/rto"
_SOURCE_URL = "https://www.eia.gov/electricity/gridmonitor/about"
_DOCUMENTATION_URL = "https://www.eia.gov/opendata/documentation.php"
_LICENSE = "U.S. Government work (public domain)"
_PAGE_SIZE = 5_000

# Current EIA-930 balancing authorities and aggregate regions. Keeping this
# explicit prevents a U.S. provider from becoming a wildcard for every zone.
_ZONES = {
    "AEC", "AECI", "AVA", "AVRN", "AZPS", "BANC", "BHBA", "BPAT",
    "CAL", "CAR", "CENT", "CHPD", "CISO", "CPLE", "CPLW", "DEAA",
    "DOPD", "DUK", "EEI", "EPE", "ERCO", "FLA", "FMPP", "FPC",
    "FPL", "GCPD", "GLHB", "GRIF", "GRID", "GVL", "GWA", "HGMA",
    "HST", "IID", "IPCO", "ISNE", "JEA", "LDWP", "LGEE", "MIDA",
    "MIDW", "MISO", "NE", "NEVP", "NSB", "NW", "NWMT", "NY",
    "NYIS", "PACE", "PACW", "PGE", "PJM", "PNM", "PSCO", "PSEI",
    "SC", "SCEG", "SCL", "SE", "SEC", "SEPA", "SIKE", "SOCO",
    "SPA", "SRP", "SW", "SWPP", "SWPW", "TAL", "TEN", "TEC",
    "TEPC", "TEX", "TIDC", "TPWR", "TVA", "US48", "WACM", "WALC",
    "WAUW", "WWA", "YAD",
}

_REGION_COLUMNS = {
    "D": COLUMN_LOAD,
    "DF": "load_forecast_mw",
    "NG": "total_generation_mw",
    "TI": "flow_net_mw",
}

_FUEL_COLUMNS = {
    "BAT": "battery_storage_mw",
    "COL": "hard_coal_mw",
    "GEO": "geothermal_mw",
    "NG": "gas_mw",
    "NUC": "nuclear_mw",
    "OES": "other_storage_mw",
    "OIL": "oil_mw",
    "OTH": "other_mw",
    "PS": "pumped_storage_mw",
    "SNB": "solar_battery_mw",
    "SUN": "solar_mw",
    "UES": "unknown_storage_mw",
    "UNK": "unknown_mw",
    "WAT": "hydro_mw",
    "WNB": "wind_battery_mw",
    "WND": "wind_mw",
}

_RENEWABLE_COLUMNS = {
    "geothermal_mw", "hydro_mw", "solar_mw", "solar_battery_mw",
    "wind_mw", "wind_battery_mw",
}


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name="utc_time"))


def _period(value: str | pd.Timestamp) -> str:
    return parse_dt(value).strftime("%Y-%m-%dT%H")


def _api_key() -> str:
    key = config.get_api_key("eia")
    if not key:
        raise RuntimeError(
            "EIA API key not configured. Register for a free key at "
            "https://www.eia.gov/opendata/register.php then run:\n"
            "  clarigrid keys set eia YOUR_KEY\n"
            "or set CLARIGRID_EIA_API_KEY."
        )
    return key


def _fetch(
    route: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    facets: dict[str, str | list[str]],
) -> list[dict[str, Any]]:
    """Fetch all pages from an EIA API v2 hourly route."""
    params: dict[str, Any] = {
        "api_key": _api_key(),
        "frequency": "hourly",
        "data[0]": "value",
        "start": _period(start),
        "end": _period(end),
        "sort[0][column]": "period",
        "sort[0][direction]": "asc",
        "length": _PAGE_SIZE,
        "offset": 0,
    }
    for name, values in facets.items():
        params[f"facets[{name}][]"] = values

    records: list[dict[str, Any]] = []
    while True:
        payload = get_json(f"{_BASE}/{route}/data/", params=params)
        response = payload.get("response") or {}
        page = response.get("data") or []
        if not isinstance(page, list):
            raise TypeError("EIA response field 'response.data' must be a list.")
        records.extend(record for record in page if isinstance(record, dict))
        total = int(response.get("total") or len(records))
        if len(records) >= total or len(page) < _PAGE_SIZE:
            break
        params["offset"] = len(records)
    return records


def _pivot(
    records: list[dict[str, Any]],
    category: str,
    columns: dict[str, str],
    *,
    multipliers: dict[str, float] | None = None,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    multipliers = multipliers or {}
    for record in records:
        code = str(record.get(category) or "").upper()
        column = columns.get(code)
        if column is None:
            continue
        timestamp = pd.to_datetime(record.get("period"), utc=True, errors="coerce")
        value = pd.to_numeric(record.get("value"), errors="coerce")
        if pd.isna(timestamp):
            continue
        rows.append({
            "utc_time": timestamp,
            "column": column,
            "value": value * multipliers.get(code, 1.0),
        })
    if not rows:
        return _empty_frame()
    frame = pd.DataFrame(rows).pivot_table(
        index="utc_time", columns="column", values="value", aggfunc="last"
    )
    frame.columns.name = None
    return normalise_index(frame.sort_index())


def _interchange_frame(records: list[dict[str, Any]]) -> pd.DataFrame:
    """Pivot neighboring BA flows and invert EIA export-positive values."""
    rows: list[dict[str, Any]] = []
    for record in records:
        neighbor = str(record.get("toba") or "").lower()
        timestamp = pd.to_datetime(record.get("period"), utc=True, errors="coerce")
        value = pd.to_numeric(record.get("value"), errors="coerce")
        if not neighbor or pd.isna(timestamp):
            continue
        rows.append({
            "utc_time": timestamp,
            "column": f"flow_{neighbor}_mw",
            "value": -value,
        })
    if not rows:
        return _empty_frame()
    frame = pd.DataFrame(rows).pivot_table(
        index="utc_time", columns="column", values="value", aggfunc="last"
    )
    frame.columns.name = None
    return normalise_index(frame.sort_index())


def _metadata(frame: pd.DataFrame, dataset: str, **extra: Any) -> pd.DataFrame:
    frame.attrs.update({
        "source_url": _SOURCE_URL,
        "documentation_url": _DOCUMENTATION_URL,
        "license": _LICENSE,
        "source_unit": "MWh per one-hour interval",
        "unit": "MW",
        "resolution": "1 hour",
        "dataset": dataset,
        **extra,
    })
    return frame


class EIAProvider(DataProvider):
    """Hourly U.S. balancing-authority operations from Form EIA-930."""

    def _region(
        self,
        zone: str,
        start: str | pd.Timestamp,
        end: str | pd.Timestamp,
        metric: str,
    ) -> pd.DataFrame:
        records = _fetch(
            "region-data", start, end,
            {"respondent": zone, "type": metric},
        )
        multiplier = {"TI": -1.0} if metric == "TI" else None
        return _pivot(records, "type", _REGION_COLUMNS, multipliers=multiplier)

    def get_prices(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        raise NotImplementedError(
            "EIA-930 does not publish wholesale market prices. Connect CAISO, "
            "NYISO, or another ISO/RTO price provider."
        )

    def get_load(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        return _metadata(
            self._region(zone, start, end, "D"), "region-data", metric="demand"
        )

    def get_load_forecast(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        return _metadata(
            self._region(zone, start, end, "DF"),
            "region-data",
            metric="day-ahead demand forecast",
        )

    def get_generation(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        records = _fetch(
            "fuel-type-data", start, end, {"respondent": zone}
        )
        return _metadata(
            _pivot(records, "fueltype", _FUEL_COLUMNS), "fuel-type-data"
        )

    def get_physical_flows(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        records = _fetch("interchange-data", start, end, {"fromba": zone})
        return _metadata(
            _interchange_frame(records),
            "interchange-data",
            sign_convention="positive=import, negative=export",
        )

    def get_net_position(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        frame = self._region(zone, start, end, "TI").rename(
            columns={"flow_net_mw": "net_position_mw"}
        )
        return _metadata(
            frame,
            "region-data",
            metric="total interchange",
            sign_convention="positive=import, negative=export",
        )

    def get_generation_share(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        generation = self.get_generation(zone, start, end)
        total = generation.sum(axis=1, min_count=1)
        shares = generation.div(total.where(total != 0), axis=0).mul(100.0)
        shares = shares.rename(columns={
            column: column.removesuffix("_mw") + "_share_pct"
            for column in shares.columns
        })
        return _metadata(shares, "fuel-type-data", unit="percent")

    def get_renewable_share(
        self, zone: str, start: str, end: str, **kwargs
    ) -> pd.DataFrame:
        generation = self.get_generation(zone, start, end)
        renewable = generation.reindex(
            columns=sorted(_RENEWABLE_COLUMNS), fill_value=0.0
        ).sum(axis=1, min_count=1)
        total = generation.sum(axis=1, min_count=1)
        frame = (renewable / total.where(total != 0) * 100.0).to_frame(
            "renewable_share_generation_pct"
        )
        return _metadata(frame, "fuel-type-data", unit="percent")

    def capabilities(self) -> set[str]:
        return {
            "load",
            "load_forecast",
            "generation",
            "physical_flows",
            "net_position",
            "generation_share",
            "renewable_share",
        }

    def zones(self) -> set[str]:
        return set(_ZONES)

    def name(self) -> str:
        return "U.S. EIA Hourly Electric Grid Monitor"


register_provider("eia", EIAProvider())
