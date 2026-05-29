"""SMARD (Bundesnetzagentur) provider — Germany electricity.

No API key required. Covers DE, AT, LU and TSO sub-zones.
Self-registers on import as ``"smard"``.

Usage::

    import clarigrid as cg
    cg.connect("smard")
    df = cg.get_prices("DE", "2025-01-01", "2025-01-07")
    df = cg.get_generation_forecast("DE", "2025-01-01", "2025-01-07")
    df = cg.get_residual_load("DE", "2025-01-01", "2025-01-07")

Supported zones: ``"DE"`` (default), ``"AT"``, ``"LU"``,
``"50hertz"``, ``"amprion"``, ``"tennet"``, ``"transnetbw"``.

Filter codes follow the authoritative bundesAPI/smard-api OpenAPI spec.
Balancing services and cross-border flows are NOT addressable through the
``chart_data`` JSON interface — use regelleistung.net / ENTSO-E instead.
"""

from __future__ import annotations

import pandas as pd

from clarigrid.core.http import get_json
from clarigrid.core.interface import DataProvider
from clarigrid.core.registry import register_provider
from clarigrid.core.types import COLUMN_LOAD, STANDARD_TZ
from clarigrid.utils.time import normalise_index, parse_dt

_BASE = "https://www.smard.de/app/chart_data"

# ── Filter codes (bundesAPI/smard-api OpenAPI spec) ─────────────────────────

# Day-ahead market prices (EUR/MWh).  All price filters MUST be queried with
# region="DE" — the filter code itself selects the bidding zone.
_PRICE_FILTERS: dict[str, int] = {
    "DE_LU": 4169,   # German day-ahead price (EPEX)
    "AT":    4170,
    "FR":    254,
    "NL":    256,
    "BE":    4996,
    "PL":    257,
    "CH":    259,
    "SI":    260,
    "CZ":    261,
    "HU":    262,
    "IT_NORD": 255,
    "DK1":   252,
    "DK2":   253,
    "NO2":   4997,
}

_FILTER_LOAD = 410        # Total grid load / Realisierter Stromverbrauch (Netzlast)
_FILTER_RESIDUAL = 4359   # Residual load
_FILTER_PUMP_CONS = 4387  # Pumped storage consumption (load side)

# Actual generation feed-in by fuel (MWh per interval).
_GENERATION_FILTERS: dict[str, int] = {
    "lignite_mw":            1223,  # Brown coal
    "nuclear_mw":            1224,
    "wind_offshore_mw":      1225,
    "hydro_mw":              1226,  # Run-of-river
    "other_conventional_mw": 1227,
    "other_renewable_mw":    1228,
    "biomass_mw":            4066,
    "wind_onshore_mw":       4067,
    "solar_mw":              4068,  # Photovoltaic
    "hard_coal_mw":          4069,
    "pumped_storage_mw":     4070,  # Generation side
    "gas_mw":                4071,  # Natural gas
}

# Day-ahead generation forecasts (MWh per interval).
# Solar PV has an upstream OpenAPI inconsistency (enum 126 vs description 125);
# both are probed at fetch time.
_GENERATION_FORECAST_FILTERS: dict[str, tuple[int, ...]] = {
    "wind_onshore_forecast_mw":  (123,),
    "wind_offshore_forecast_mw": (3791,),
    "solar_forecast_mw":         (125, 126),
    "wind_solar_forecast_mw":    (5097,),
    "other_forecast_mw":         (715,),
}

# region= path segment.  Casing is significant (API rejects lowercase).
_REGION_MAP: dict[str, str] = {
    "DE":         "DE",
    "DE_LU":      "DE-LU",
    "DE_AT_LU":   "DE-AT-LU",
    "AT":         "AT",
    "LU":         "LU",
    "50HERTZ":    "50Hertz",
    "AMPRION":    "Amprion",
    "TENNET":     "TenneT",
    "TRANSNETBW": "TransnetBW",
    "APG":        "APG",
    "CREOS":      "Creos",
}

_WEEK_MS = 7 * 24 * 3600 * 1000


def _resolve_region(zone: str) -> str:
    return _REGION_MAP.get(zone.upper().replace("-", "_"), zone)


def _get_index(filter_code: int, region: str, resolution: str) -> list[int]:
    """Fetch available week-start timestamps for a filter/region/resolution."""
    url = f"{_BASE}/{filter_code}/{region}/index_{resolution}.json"
    data = get_json(url)
    if isinstance(data, list):
        return data
    return data.get("timestamps", [])


def _get_week(filter_code: int, region: str, resolution: str, ts_ms: int) -> list[list]:
    """Fetch series data for one week."""
    url = f"{_BASE}/{filter_code}/{region}/{filter_code}_{region}_{resolution}_{ts_ms}.json"
    data = get_json(url)
    return data.get("series", [])


def _fetch_series(
    filter_code: int,
    region: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    resolution: str = "hour",
) -> pd.Series:
    """Return a Series indexed by UTC timestamp for the given filter and range.

    *region* must already be resolved to the SMARD path segment.
    """
    start_ts = parse_dt(start)
    end_ts = parse_dt(end)
    start_ms = int(start_ts.timestamp() * 1000)
    end_ms = int(end_ts.timestamp() * 1000)

    index_timestamps = _get_index(filter_code, region, resolution)
    relevant = [
        ts for ts in index_timestamps
        if ts <= end_ms and ts + _WEEK_MS > start_ms
    ]

    all_points: list[list] = []
    for ts in relevant:
        all_points.extend(_get_week(filter_code, region, resolution, ts))

    if not all_points:
        return pd.Series(dtype=float, name="value")

    df = pd.DataFrame(all_points, columns=["ts_ms", "value"])
    df["ts_ms"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    df = df.set_index("ts_ms")
    df.index.name = "utc_time"
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df["value"].loc[start_ts:end_ts]


def _fetch_first_available(
    filter_codes: tuple[int, ...],
    region: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    resolution: str = "hour",
) -> pd.Series:
    """Try each filter code until one returns data (for 125/126 ambiguity)."""
    for code in filter_codes:
        try:
            s = _fetch_series(code, region, start, end, resolution)
            if not s.empty:
                return s
        except Exception:
            continue
    return pd.Series(dtype=float, name="value")


def _frame_from_filters(
    filters: dict[str, int] | dict[str, tuple[int, ...]],
    region: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
) -> pd.DataFrame:
    """Build a wide DataFrame from a {column: filter_code(s)} mapping."""
    frames: dict[str, pd.Series] = {}
    for col, code in filters.items():
        codes = code if isinstance(code, tuple) else (code,)
        try:
            s = _fetch_first_available(codes, region, start, end)
            if not s.empty:
                frames[col] = s
        except Exception:
            # Some fuel types / forecasts unavailable for certain regions.
            pass
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, axis=1)
    df.columns = list(frames.keys())
    return normalise_index(df, STANDARD_TZ)


class SmardProvider(DataProvider):
    """Bundesnetzagentur SMARD data portal — Germany electricity."""

    # ── Prices ──────────────────────────────────────────────────────────
    def get_prices(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        z = zone.upper().replace("-", "_")
        filter_code = _PRICE_FILTERS.get(z)
        if filter_code is None:
            raise ValueError(
                f"SMARD has no day-ahead price series for zone '{zone}'. "
                f"Supported price zones: {sorted(_PRICE_FILTERS)}."
            )
        # Price filters are always queried with region=DE.
        s = _fetch_series(filter_code, "DE", start, end)
        return normalise_index(s.rename("price_eur_mwh").to_frame(), STANDARD_TZ)

    # ── Load ────────────────────────────────────────────────────────────
    def get_load(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        s = _fetch_series(_FILTER_LOAD, _resolve_region(zone), start, end)
        return normalise_index(s.rename(COLUMN_LOAD).to_frame(), STANDARD_TZ)

    def get_residual_load(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        """Residual load (load minus renewable feed-in) + pumped-storage consumption."""
        region = _resolve_region(zone)
        return _frame_from_filters(
            {
                "residual_load_mw": _FILTER_RESIDUAL,
                "pumped_storage_consumption_mw": _FILTER_PUMP_CONS,
            },
            region, start, end,
        )

    # ── Generation ──────────────────────────────────────────────────────
    def get_generation(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        return _frame_from_filters(_GENERATION_FILTERS, _resolve_region(zone), start, end)

    def get_generation_forecast(self, zone: str, start: str, end: str, **kwargs) -> pd.DataFrame:
        """Day-ahead wind/solar generation forecast (MW)."""
        return _frame_from_filters(
            _GENERATION_FORECAST_FILTERS, _resolve_region(zone), start, end
        )

    # ── Declarations ────────────────────────────────────────────────────
    def zones(self) -> set[str]:
        # German control areas — these carry the full capability set (prices,
        # load, generation, forecasts).  The neighbour-zone day-ahead prices in
        # _PRICE_FILTERS (FR, NL, BE …) are NOT advertised here to avoid the
        # router sending load/generation requests to SMARD for those zones;
        # fetch them explicitly with get_prices(zone, source="smard").
        return {"DE", "DE_LU", "AT", "LU", "50HERTZ", "AMPRION", "TENNET", "TRANSNETBW"}

    def capabilities(self) -> set[str]:
        return {
            "prices", "load", "generation",
            "generation_forecast", "residual_load",
        }

    def name(self) -> str:
        return "SMARD (Bundesnetzagentur)"


def register() -> None:
    """Register the SMARD provider. Called automatically on import."""
    register_provider("smard", SmardProvider())


register()
