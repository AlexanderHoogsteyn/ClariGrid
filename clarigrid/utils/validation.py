"""Input validation helpers."""

from __future__ import annotations

import pandas as pd

from clarigrid.core.types import ZONE_ALIASES


def resolve_zone(zone: str) -> str:
    """Normalise zone string to canonical ENTSO-E bidding zone code."""
    upper = zone.upper().replace("-", "_")
    return ZONE_ALIASES.get(upper, upper)


def validate_date_range(start: str | pd.Timestamp, end: str | pd.Timestamp) -> None:
    """Raise ValueError if range is invalid."""
    s = pd.Timestamp(start)
    e = pd.Timestamp(end)
    if e <= s:
        raise ValueError(f"end ({e}) must be after start ({s}).")
