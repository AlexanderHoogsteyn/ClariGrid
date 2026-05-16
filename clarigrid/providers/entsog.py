"""ENTSOG Transparency Platform provider — EU gas transmission data.

No API key required. Self-registers on import as ``"entsog"``.

Usage::

    import clarigrid as cg
    cg.connect("entsog")
    df = cg.get_gas_flows("BE-TSO-0001", "2026-05-01", "2026-05-07")

Zone / operator key examples:
    - ``"BE-TSO-0001"``  Fluxys Belgium
    - ``"NL-TSO-0001"``  Gasunie
    - ``"DE-TSO-0015"``  Fluxys TENP (Germany)
    - ``"BE"``           All Belgian operators (country filter)
"""

from __future__ import annotations

import pandas as pd

from clarigrid.core.http import paginate_offset
from clarigrid.core.interface import GasDataProvider
from clarigrid.core.registry import register_provider
from clarigrid.utils.time import normalise_index, parse_dt

_BASE = "https://transparency.entsog.eu/api/v1"

# Map 2-letter country codes to one representative operator for simple queries.
# Users wanting all operators should pass the operator key directly.
_COUNTRY_OPERATOR: dict[str, str] = {
    "BE": "BE-TSO-0001",
    "NL": "NL-TSO-0001",
    "FR": "FR-TSO-0001",
    "DE": "DE-TSO-0015",
    "AT": "AT-TSO-0001",
    "IT": "IT-TSO-0001",
    "ES": "ES-TSO-0001",
    "PT": "PT-TSO-0001",
    "PL": "PL-TSO-0001",
    "CZ": "CZ-TSO-0001",
    "SK": "SK-TSO-0001",
    "HU": "HU-TSO-0001",
    "RO": "RO-TSO-0001",
    "BG": "BG-TSO-0001",
    "CH": "CH-TSO-0001",
    "DK": "DK-TSO-0001",
    "NO": "NO-TSO-0001",
    "FI": "FI-TSO-0001",
    "LT": "LT-TSO-0001",
    "LV": "LV-TSO-0001",
    "EE": "EE-TSO-0001",
}


def _resolve_zone(zone: str) -> dict[str, str]:
    """Return additional query params for the zone argument.

    Detection order:
    1. 2-letter country code → map to primary operator key.
    2. Operator key pattern ``XX-TSO-XXXX`` → operatorKey filter.
    3. Anything else (e.g. ENTSOG point keys like ``IZT-00089``) → pointKey.
    """
    if len(zone) == 2 and zone.upper() in _COUNTRY_OPERATOR:
        return {"operatorKey": _COUNTRY_OPERATOR[zone.upper()]}
    if "-TSO-" in zone.upper():
        return {"operatorKey": zone}
    # ENTSOG point keys (e.g. IZT-00089, CZT-00041)
    return {"pointKey": zone}


def _fetch_operational(
    indicator: str,
    period_type: str,
    start: str,
    end: str,
    extra_params: dict,
) -> list[dict]:
    params = {
        "indicator": indicator,
        "from": pd.Timestamp(start).strftime("%Y-%m-%d"),
        "to": pd.Timestamp(end).strftime("%Y-%m-%d"),
        "periodType": period_type,
        **extra_params,
    }
    records: list[dict] = []
    for page in paginate_offset(
        f"{_BASE}/operationalData",
        params,
        data_key="operationalData",
        limit=10_000,
        throttle=0.3,
    ):
        records.extend(page)
    return records


def _records_to_df(records: list[dict]) -> pd.DataFrame:
    """Convert raw ENTSOG operational data records to a normalised DataFrame."""
    if not records:
        return pd.DataFrame(columns=["flow_kwh_d", "direction", "point_key", "operator_key"])

    rows = []
    for r in records:
        # Skip NA records (value is empty string when isNA == 1).
        if r.get("isNA") == 1 or r.get("value") in (None, "", "NA"):
            continue
        try:
            ts_raw = r.get("periodFrom", "")
            ts = pd.Timestamp(ts_raw)
            # ENTSOG timestamps carry local TZ offset (e.g. +02:00) — convert, not localize.
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            else:
                ts = ts.tz_convert("UTC")
            val = float(r["value"])
        except (KeyError, ValueError, TypeError):
            continue
        rows.append(
            {
                "utc_time": ts,
                "flow_kwh_d": val,
                "direction": r.get("directionKey", ""),
                "point_key": r.get("pointKey", ""),
                "operator_key": r.get("operatorKey", ""),
                "unit": r.get("unit", "kWh/d"),
            }
        )

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows).set_index("utc_time")
    df.index.name = "utc_time"
    return df


class EntsogProvider(GasDataProvider):
    """ENTSOG Transparency Platform — EU gas TSO data."""

    def get_gas_flows(
        self,
        zone: str,
        start: str,
        end: str,
        *,
        indicator: str = "Physical Flow",
        period_type: str = "day",
        **kwargs,
    ) -> pd.DataFrame:
        zone_params = _resolve_zone(zone)
        records = _fetch_operational(indicator, period_type, start, end, zone_params)
        return _records_to_df(records)

    def get_capacity(
        self,
        zone: str,
        start: str,
        end: str,
        **kwargs,
    ) -> pd.DataFrame:
        zone_params = _resolve_zone(zone)
        records = _fetch_operational("Firm Technical", "day", start, end, zone_params)
        df = _records_to_df(records)
        if not df.empty and "flow_kwh_d" in df.columns:
            df = df.rename(columns={"flow_kwh_d": "capacity_kwh_d"})
        return df

    def zones(self) -> set[str]:
        # ENTSOG accepts any operator key, country code, or point key at
        # runtime — wildcard matches all zones.
        return {"*"}

    def name(self) -> str:
        return "ENTSOG Transparency Platform"


def register() -> None:
    """Register the ENTSOG provider. Called automatically on import."""
    register_provider("entsog", EntsogProvider())


register()
