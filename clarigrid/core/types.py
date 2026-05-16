"""Shared data structures and constants."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

STANDARD_TZ = "UTC"

COLUMN_PRICE = "price_mwh"
COLUMN_LOAD = "load_mw"

# Canonical bidding zone aliases — map common variants to ENTSO-E codes.
ZONE_ALIASES: dict[str, str] = {
    "DE": "DE_LU",
    "GERMANY": "DE_LU",
    "BELGIUM": "BE",
    "FRANCE": "FR",
    "NETHERLANDS": "NL",
    "SPAIN": "ES",
    "ITALY_NORTH": "IT_NORD",
}


@dataclass
class ProviderMeta:
    name: str
    version: str = "0.0.0"
    homepage: str = ""
    requires_api_key: bool = False
    supported_zones: list[str] = field(default_factory=list)
    supported_datasets: list[str] = field(default_factory=list)
