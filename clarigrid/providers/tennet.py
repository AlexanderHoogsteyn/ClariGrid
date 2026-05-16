"""TenneT NL provider — Netherlands electricity imbalance & metered load.

Requires a **free** API key. Register at https://developer.tennet.eu/register/
Then store the key:

    clarigrid keys set tennet YOUR_KEY
    # or: export CLARIGRID_TENNET_API_KEY=YOUR_KEY

Self-registers on import as ``"tennet"``.

Usage::

    import clarigrid as cg
    cg.connect("tennet")
    df = cg.get_prices("NL", "2026-05-01", "2026-05-07")  # settlement prices
    df = cg.get_load("NL", "2026-05-01", "2026-05-07")    # metered injections

Notes:
    - ``get_prices`` returns **imbalance settlement prices** (up/down regulation)
      in columns ``up_regulation_price_eur_mwh`` and
      ``down_regulation_price_eur_mwh`` — NOT day-ahead prices.
    - ``zone`` is ignored; data is always NL-wide.
    - Time resolution is 15-minute PTU (Programme Time Unit).
    - The legacy tennet.org XML API was decommissioned in December 2024.

API spec: https://developer.tennet.eu/specs/
"""

from __future__ import annotations

import pandas as pd

from clarigrid.core.http import paginate_pages
from clarigrid.core.interface import DataProvider
from clarigrid.core.registry import register_provider
from clarigrid.core.types import COLUMN_LOAD, STANDARD_TZ
from clarigrid.utils.time import normalise_index

_BASE = "https://api.tennet.eu/v1"
_PAGE_SIZE = 500


def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def _fetch(endpoint: str, params: dict, api_key: str) -> list[dict]:
    """Paginate a TenneT REST endpoint.

    TenneT uses Spring Boot page wrappers: ``{content: [...], totalPages: N,
    page: 0, size: 500}``. Pages start at 0.
    """
    records: list[dict] = []
    for page in paginate_pages(
        f"{_BASE}/{endpoint}",
        params,
        data_key="content",
        page_param="page",
        size_param="size",
        page_size=_PAGE_SIZE,
        start_page=0,
        total_pages_key="totalPages",
        throttle=0.1,
        headers=_headers(api_key),
    ):
        records.extend(page)
    return records


def _parse_ts(raw: str) -> pd.Timestamp:
    """Parse ISO 8601 timestamp with TZ offset to UTC."""
    ts = pd.Timestamp(raw)
    if ts.tzinfo is None:
        # Assume CET/CEST if no timezone info in response.
        ts = ts.tz_localize("Europe/Amsterdam", ambiguous=False, nonexistent="shift_forward")
    return ts.tz_convert("UTC")


def _get_time(record: dict) -> pd.Timestamp | None:
    """Try multiple candidate timestamp field names."""
    for field in ("dateTime", "ptuStart", "settlementTime", "startDatetime", "startDateTime", "timestamp"):
        val = record.get(field)
        if val:
            try:
                return _parse_ts(val)
            except Exception:
                continue
    return None


class TennetProvider(DataProvider):
    """TenneT NL — Netherlands imbalance settlement prices and metered load."""

    def __init__(self) -> None:
        from clarigrid.core import config as _config
        key = _config.get_api_key("tennet")
        if not key:
            raise RuntimeError(
                "TenneT API key not found. Register for free at "
                "https://developer.tennet.eu/register/ then run:\n"
                "  clarigrid keys set tennet YOUR_KEY\n"
                "or set CLARIGRID_TENNET_API_KEY environment variable."
            )
        self._api_key = key

    def get_prices(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        """Return imbalance settlement prices (up/down regulation) in EUR/MWh.

        Columns:
            ``up_regulation_price_eur_mwh`` — price for activating upward regulation
            ``down_regulation_price_eur_mwh`` — price for activating downward regulation

        Note: these are **imbalance settlement prices**, not day-ahead market prices.
        """
        params = {
            "dateFrom": pd.Timestamp(start).strftime("%Y-%m-%d"),
            "dateTo": pd.Timestamp(end).strftime("%Y-%m-%d"),
        }
        records = _fetch("settlement-prices", params, self._api_key)
        if not records:
            return pd.DataFrame()

        rows: list[dict] = []
        for r in records:
            ts = _get_time(r)
            if ts is None:
                continue
            try:
                # Try multiple field name variants for up/down prices.
                up = float(
                    r.get("upwardSettlementPrice")
                    or r.get("upwardRegulatingPrice")
                    or r.get("uprp")
                    or r.get("upward_price")
                    or float("nan")
                )
                down = float(
                    r.get("downwardSettlementPrice")
                    or r.get("downwardRegulatingPrice")
                    or r.get("downrp")
                    or r.get("downward_price")
                    or float("nan")
                )
            except (TypeError, ValueError):
                continue
            rows.append({
                "utc_time": ts,
                "up_regulation_price_eur_mwh": up,
                "down_regulation_price_eur_mwh": down,
            })

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows).set_index("utc_time")
        df.index.name = "utc_time"
        return normalise_index(df, STANDARD_TZ)

    def get_load(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        """Return metered injections (feed-in − export + import) in MW.

        Source: ``/metered-injections`` endpoint.
        Column: ``load_mw``.
        """
        params = {
            "dateFrom": pd.Timestamp(start).strftime("%Y-%m-%d"),
            "dateTo": pd.Timestamp(end).strftime("%Y-%m-%d"),
        }
        records = _fetch("metered-injections", params, self._api_key)
        if not records:
            return pd.DataFrame()

        rows: list[dict] = []
        for r in records:
            ts = _get_time(r)
            if ts is None:
                continue
            try:
                # Try multiple candidate field names.
                val = (
                    r.get("measuredValue")
                    or r.get("meteredInjection")
                    or r.get("injectedVolume")
                    or r.get("load")
                    or r.get("volume")
                )
                val = float(val) if val is not None else float("nan")
            except (TypeError, ValueError):
                val = float("nan")
            rows.append({"utc_time": ts, COLUMN_LOAD: val})

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows).set_index("utc_time")
        df.index.name = "utc_time"
        return normalise_index(df, STANDARD_TZ)

    def get_generation(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        raise NotImplementedError(
            "TenneT NL does not provide a generation mix endpoint. "
            "Use ENTSO-E for NL generation data."
        )

    def capabilities(self) -> set[str]:
        return {"prices", "load"}

    def name(self) -> str:
        return "TenneT NL (developer.tennet.eu)"


def register() -> None:
    """Register the TenneT provider. Called automatically on import."""
    try:
        register_provider("tennet", TennetProvider())
    except RuntimeError:
        # No API key configured — register a stub that raises on use.
        register_provider("tennet", _TennetStub())


class _TennetStub(DataProvider):
    """Placeholder when no TenneT API key is configured."""

    def get_prices(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        raise RuntimeError(
            "TenneT API key not configured. Register at "
            "https://developer.tennet.eu/register/ then:\n"
            "  clarigrid keys set tennet YOUR_KEY"
        )

    def get_load(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        return self.get_prices(zone, start, end)

    def get_generation(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        return self.get_prices(zone, start, end)

    def capabilities(self) -> set[str]:
        return set()

    def name(self) -> str:
        return "TenneT NL (no key — stub)"


register()
