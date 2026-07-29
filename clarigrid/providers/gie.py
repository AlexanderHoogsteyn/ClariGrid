"""Gas Infrastructure Europe AGSI and ALSI provider.

GIE's API is free after registration.  One key can grant access to AGSI
(underground gas storage), ALSI (LNG terminals), or both.  Configure it with
``clarigrid keys set gie YOUR_KEY``.
"""

from __future__ import annotations

import time
from typing import Any

import pandas as pd

from clarigrid.core import config
from clarigrid.core.http import get_json
from clarigrid.core.interface import DataProvider
from clarigrid.core.registry import register_provider
from clarigrid.utils.time import normalise_index

_AGSI = "https://agsi.gie.eu/api"
_ALSI = "https://alsi.gie.eu/api"
_LICENSE = "GIE open data; attribution required"
_PAGE_SIZE = 300

_COUNTRY_ALIASES = {
    "DE_LU": "DE",
    "DE_AT_LU": "DE",
    "DK1": "DK",
    "DK2": "DK",
    "IT_NORD": "IT",
    "GB": "UK",
}


def _scope(zone: str) -> dict[str, str]:
    upper = zone.upper()
    if upper in {"EU", "NE", "AI"}:
        return {"type": upper.lower()}
    return {"country": _COUNTRY_ALIASES.get(upper, upper)}


def _fetch(
    base_url: str,
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    api_key: str,
    *,
    company: str | None = None,
    facility: str | None = None,
) -> list[dict]:
    params: dict[str, Any] = {
        **_scope(zone),
        "from": pd.Timestamp(start).strftime("%Y-%m-%d"),
        "to": pd.Timestamp(end).strftime("%Y-%m-%d"),
        "page": 1,
        "size": _PAGE_SIZE,
    }
    if company:
        params["company"] = company
    if facility:
        if not company:
            raise ValueError("GIE facility queries also require company=<operator EIC>.")
        params["facility"] = facility

    records: list[dict] = []
    while True:
        payload = get_json(base_url, params=params, headers={"x-key": api_key})
        if not isinstance(payload, dict):
            raise TypeError("GIE returned an unexpected non-object response.")
        page = payload.get("data") or []
        if not isinstance(page, list):
            raise TypeError("GIE response field 'data' must be a list.")
        records.extend(item for item in page if isinstance(item, dict))
        if int(params["page"]) >= int(payload.get("last_page", 1)):
            break
        params["page"] += 1
        time.sleep(1.05)  # GIE permits at most 60 calls per minute.
    return records


def _number(record: dict, field: str, multiplier: float = 1.0) -> float:
    value = pd.to_numeric(record.get(field), errors="coerce")
    return float(value * multiplier) if not pd.isna(value) else float("nan")


def _frame(records: list[dict], *, kind: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for record in records:
        timestamp = pd.to_datetime(record.get("gasDayStart"), utc=True, errors="coerce")
        if pd.isna(timestamp):
            continue
        common = {
            "utc_time": timestamp,
            "entity_name": record.get("name"),
            "entity_code": record.get("code"),
            "data_status": record.get("status"),
        }
        if kind == "storage":
            row = {
                **common,
                # TWh -> MWh
                "gas_in_storage_mwh": _number(record, "gasInStorage", 1_000_000.0),
                "working_gas_volume_mwh": _number(record, "workingGasVolume", 1_000_000.0),
                "annual_consumption_mwh": _number(record, "consumption", 1_000_000.0),
                # GWh/day -> MWh/day
                "injection_mwh_d": _number(record, "injection", 1_000.0),
                "withdrawal_mwh_d": _number(record, "withdrawal", 1_000.0),
                "net_withdrawal_mwh_d": _number(record, "netWithdrawal", 1_000.0),
                "injection_capacity_mwh_d": _number(record, "injectionCapacity", 1_000.0),
                "withdrawal_capacity_mwh_d": _number(record, "withdrawalCapacity", 1_000.0),
                "storage_full_pct": _number(record, "full"),
            }
        elif kind == "lng":
            row = {
                **common,
                "lng_inventory_thousand_m3": _number(record, "inventory"),
                "lng_capacity_thousand_m3": _number(record, "dtmi"),
                "sendout_mwh_d": _number(record, "sendOut", 1_000.0),
                "sendout_capacity_mwh_d": _number(record, "dtrs", 1_000.0),
            }
        else:
            raise ValueError(f"Unknown GIE frame kind: {kind!r}")
        rows.append(row)

    if not rows:
        return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name="utc_time"))
    result = pd.DataFrame(rows).set_index("utc_time").sort_index()
    result = result[~result.index.duplicated(keep="last")]
    return normalise_index(result)


def _metadata(frame: pd.DataFrame, platform: str) -> pd.DataFrame:
    frame.attrs.update({
        "source_url": _AGSI if platform == "AGSI" else _ALSI,
        "license": _LICENSE,
        "attribution": f"GIE {platform}",
        "frequency": "daily",
    })
    return frame


class GieProvider(DataProvider):
    """GIE daily European gas storage and LNG transparency data."""

    def _api_key(self) -> str:
        key = config.get_api_key("gie")
        if not key:
            raise RuntimeError(
                "GIE API key not configured. Register for free at "
                "https://agsi.gie.eu/account then run:\n"
                "  clarigrid keys set gie YOUR_KEY\n"
                "or set CLARIGRID_GIE_API_KEY."
            )
        return key

    def get_gas_storage(
        self,
        zone: str,
        start: str,
        end: str,
        *,
        company: str | None = None,
        facility: str | None = None,
        **kwargs,
    ) -> pd.DataFrame:
        records = _fetch(
            _AGSI, zone, start, end, self._api_key(), company=company, facility=facility
        )
        return _metadata(_frame(records, kind="storage"), "AGSI")

    def get_lng_inventory(
        self,
        zone: str,
        start: str,
        end: str,
        *,
        company: str | None = None,
        facility: str | None = None,
        **kwargs,
    ) -> pd.DataFrame:
        records = _fetch(
            _ALSI, zone, start, end, self._api_key(), company=company, facility=facility
        )
        return _metadata(_frame(records, kind="lng"), "ALSI")

    def get_prices(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        raise NotImplementedError("GIE does not publish gas or electricity prices.")

    def get_load(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        raise NotImplementedError("GIE is a gas infrastructure data provider.")

    def get_generation(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        raise NotImplementedError("GIE is a gas infrastructure data provider.")

    def capabilities(self) -> set[str]:
        return {"gas_storage", "lng_inventory"}

    def zones(self) -> set[str]:
        return {"*"}

    def name(self) -> str:
        return "Gas Infrastructure Europe (AGSI/ALSI)"


register_provider("gie", GieProvider())
