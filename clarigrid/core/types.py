"""Shared data structures and constants."""

from __future__ import annotations

STANDARD_TZ = "UTC"

# ── Canonical column names ─────────────────────────────────────────────────
# Single source of truth — import these everywhere instead of using literals.

COLUMN_PRICE    = "price_mwh"    # electricity price; currency in EnergyFrame.currency
COLUMN_LOAD     = "load_mw"      # actual consumption / injection
COLUMN_GAS_FLOW = "flow_kwh_d"   # gas physical flow (kWh per day)
COLUMN_GAS_CAP  = "capacity_kwh_d"  # gas firm technical capacity (kWh per day)

# ── Bidding zone aliases ───────────────────────────────────────────────────
# Maps common variants to canonical ENTSO-E codes.

ZONE_ALIASES: dict[str, str] = {
    "DE": "DE_LU",
    "GERMANY": "DE_LU",
    "BELGIUM": "BE",
    "FRANCE": "FR",
    "NETHERLANDS": "NL",
    "SPAIN": "ES",
    "ITALY_NORTH": "IT_NORD",
    "CAISO": "CISO",
    "ERCOT": "ERCO",
    "NYISO": "NYIS",
    "ISO-NE": "ISNE",
    "ISO_NE": "ISNE",
    "SPP": "SWPP",
    "US": "US48",
    "USA": "US48",
}
