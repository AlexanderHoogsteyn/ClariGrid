"""Time parsing and normalisation helpers."""

from __future__ import annotations

import pandas as pd


def parse_dt(value: str | pd.Timestamp, tz: str = "UTC") -> pd.Timestamp:
    """Parse a date/datetime string to a tz-aware Timestamp."""
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize(tz)
    else:
        ts = ts.tz_convert(tz)
    return ts


def normalise_index(df: pd.DataFrame, tz: str = "UTC") -> pd.DataFrame:
    """Ensure df has a tz-aware DatetimeIndex in *tz*."""
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("DataFrame must have a DatetimeIndex.")
    if df.index.tzinfo is None:
        df.index = df.index.tz_localize(tz)
    else:
        df.index = df.index.tz_convert(tz)
    df.index.name = "utc_time"
    return df


def date_range_str(start: str | pd.Timestamp, end: str | pd.Timestamp) -> str:
    """Compact key string for cache keying."""
    s = parse_dt(start)
    e = parse_dt(end)
    return f"{s.strftime('%Y%m%d')}_{e.strftime('%Y%m%d')}"
