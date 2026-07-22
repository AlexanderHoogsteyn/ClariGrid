"""California ISO OASIS day-ahead price provider.

OASIS returns a ZIP archive containing one or more CSV files. The public API
uses UTC query boundaries even though trading dates are shown in Pacific time.
Clarigrid exposes stable CAISO trading-hub zones and also accepts an arbitrary
OASIS node through the public ``node=`` argument.
"""

from __future__ import annotations

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

_BASE = "https://oasis.caiso.com/oasisapi/SingleZip"
_SOURCE_URL = (
    "https://www.caiso.com/systems-applications/portals-applications/"
    "open-access-same-time-information-system-oasis"
)
_DOCUMENTATION_URL = (
    "https://www.caiso.com/Documents/"
    "OASIS-InterfaceSpecification_v5_1_2Redline_Fall2017Release.pdf"
)
_LICENSE = "CAISO OASIS terms of use"

_HUB_NODES = {
    "CISO_NP15": "TH_NP15_GEN-APND",
    "CISO_SP15": "TH_SP15_GEN-APND",
    "CISO_ZP26": "TH_ZP26_GEN-APND",
}


def _query_time(value: str | pd.Timestamp) -> str:
    return parse_dt(value).strftime("%Y%m%dT%H:%M-0000")


def _chunks(
    start: str | pd.Timestamp, end: str | pd.Timestamp
) -> Iterator[tuple[pd.Timestamp, pd.Timestamp]]:
    cursor = parse_dt(start)
    final = parse_dt(end)
    if final <= cursor:
        final = cursor + pd.Timedelta(days=1)
    while cursor < final:
        chunk_end = min(cursor + pd.Timedelta(days=1), final)
        yield cursor, chunk_end
        cursor = chunk_end


def _read_zip(payload: bytes) -> pd.DataFrame:
    """Read and concatenate all OASIS CSV members from a ZIP response."""
    try:
        with ZipFile(BytesIO(payload)) as archive:
            csv_names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if not csv_names:
                details = ", ".join(archive.namelist()) or "empty archive"
                raise ProviderUnavailableError(
                    f"CAISO OASIS returned no CSV result ({details})."
                )
            frames = [pd.read_csv(archive.open(name)) for name in csv_names]
    except BadZipFile as exc:
        preview = payload[:300].decode("utf-8", errors="replace")
        raise ProviderUnavailableError(
            f"CAISO OASIS returned an invalid ZIP response: {preview}"
        ) from exc
    return pd.concat(frames, ignore_index=True)


def _price_frame(raw: pd.DataFrame, node: str) -> pd.DataFrame:
    """Extract the total LMP component and return canonical provider output."""
    if raw.empty:
        return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name="utc_time"))
    required = {"INTERVALSTARTTIME_GMT", "XML_DATA_ITEM", "MW"}
    missing = required.difference(raw.columns)
    if missing:
        raise TypeError(f"CAISO price CSV is missing columns: {sorted(missing)}")

    data = raw.loc[raw["XML_DATA_ITEM"].astype(str).str.upper() == "LMP_PRC"].copy()
    if "NODE" in data.columns:
        data = data.loc[data["NODE"].astype(str) == node]
    elif "NODE_ID" in data.columns:
        data = data.loc[data["NODE_ID"].astype(str) == node]
    data["utc_time"] = pd.to_datetime(
        data["INTERVALSTARTTIME_GMT"], utc=True, errors="coerce"
    )
    data["price_usd_mwh"] = pd.to_numeric(data["MW"], errors="coerce")
    data = data.dropna(subset=["utc_time"])
    frame = data.groupby("utc_time")["price_usd_mwh"].last().to_frame().sort_index()
    return normalise_index(frame)


def _fetch_prices(
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    node: str,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for chunk_start, chunk_end in _chunks(start, end):
        payload = get_bytes(
            _BASE,
            params={
                "resultformat": 6,
                "queryname": "PRC_LMP",
                "version": 12,
                "startdatetime": _query_time(chunk_start),
                "enddatetime": _query_time(chunk_end),
                "market_run_id": "DAM",
                "node": node,
            },
            timeout=60,
        )
        frames.append(_price_frame(_read_zip(payload), node))
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name="utc_time"))
    frame = pd.concat(frames).sort_index()
    return normalise_index(frame.loc[~frame.index.duplicated(keep="last")])


class CAISOProvider(DataProvider):
    """CAISO day-ahead locational marginal prices from OASIS."""

    def get_prices(
        self,
        zone: str,
        start: str,
        end: str,
        *,
        node: str | None = None,
        market: str = "day_ahead",
        **kwargs: Any,
    ) -> pd.DataFrame:
        if market != "day_ahead":
            raise ValueError(
                "CAISO currently supports market='day_ahead' only."
            )
        selected_node = node or _HUB_NODES.get(zone)
        if not selected_node:
            hubs = ", ".join(sorted(_HUB_NODES))
            raise ValueError(
                f"CAISO prices require a node. Use one of {hubs}, or pass "
                "node='YOUR_OASIS_NODE'."
            )
        frame = _fetch_prices(start, end, selected_node)
        frame.attrs.update({
            "currency": "USD",
            "unit": "USD/MWh",
            "market": market,
            "location": selected_node,
            "source_url": _SOURCE_URL,
            "documentation_url": _DOCUMENTATION_URL,
            "license": _LICENSE,
        })
        return frame

    def get_load(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        raise NotImplementedError(
            "CAISO OASIS load support is not implemented. Connect 'eia' for "
            "CISO balancing-authority load."
        )

    def get_generation(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        raise NotImplementedError(
            "CAISO OASIS generation support is not implemented. Connect 'eia' "
            "for CISO generation by fuel."
        )

    def capabilities(self) -> set[str]:
        return {"prices"}

    def zones(self) -> set[str]:
        return set(_HUB_NODES)

    def name(self) -> str:
        return "California ISO OASIS"


register_provider("caiso", CAISOProvider())
