"""Filesystem cache for provider responses."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pandas as pd

from clarigrid.utils.time import date_range_str

_CACHE_DIR = Path(os.environ.get("CLARIGRID_CACHE_DIR", Path.home() / ".clarigrid" / "cache"))


def _cache_key(provider: str, dataset: str, zone: str, start, end) -> str:
    raw = f"{provider}_{dataset}_{zone}_{date_range_str(start, end)}"
    return hashlib.md5(raw.encode()).hexdigest()[:16] + f"_{provider}_{dataset}_{zone}"


def _path(key: str) -> Path:
    return _CACHE_DIR / f"{key}.parquet"


def load(provider: str, dataset: str, zone: str, start, end) -> pd.DataFrame | None:
    """Return cached DataFrame or None if absent/stale."""
    p = _path(_cache_key(provider, dataset, zone, start, end))
    if p.exists():
        return pd.read_parquet(p)
    return None


def save(df: pd.DataFrame, provider: str, dataset: str, zone: str, start, end) -> None:
    """Persist a DataFrame to the cache."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(_cache_key(provider, dataset, zone, start, end))
    df.to_parquet(p)


def clear(provider: str | None = None) -> int:
    """Delete cache files. If provider given, only that provider's files."""
    if not _CACHE_DIR.exists():
        return 0
    removed = 0
    for f in _CACHE_DIR.glob("*.parquet"):
        if provider is None or f"_{provider}_" in f.name:
            f.unlink()
            removed += 1
    return removed
