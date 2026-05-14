"""Filesystem cache for provider responses.

Parquet is used when available (pyarrow or fastparquet).  If neither is
installed the cache silently no-ops — all ``load()`` calls return ``None``
and ``save()`` emits a one-time ``UserWarning`` so users know caching is off.
Install pyarrow to enable: ``pip install pyarrow``
"""

from __future__ import annotations

import hashlib
import os
import warnings
from pathlib import Path

import pandas as pd

from clarigrid.utils.time import date_range_str

_CACHE_DIR = Path(os.environ.get("CLARIGRID_CACHE_DIR", Path.home() / ".clarigrid" / "cache"))

# ---------------------------------------------------------------------------
# Detect parquet engine availability once at import time.
# Supports both pyarrow and fastparquet so either install works.
# ---------------------------------------------------------------------------
_PARQUET_OK: bool = False
try:
    import pyarrow  # noqa: F401
    _PARQUET_OK = True
except ImportError:
    try:
        import fastparquet  # noqa: F401
        _PARQUET_OK = True
    except ImportError:
        pass  # cache will be silently disabled

_warned: bool = False


def _warn_once() -> None:
    global _warned
    if not _warned:
        warnings.warn(
            "Clarigrid cache disabled: neither pyarrow nor fastparquet is installed. "
            "Queries will always hit the network. "
            "To enable caching run:  pip install pyarrow",
            UserWarning,
            stacklevel=4,
        )
        _warned = True


def _cache_key(provider: str, dataset: str, zone: str, start, end) -> str:
    raw = f"{provider}_{dataset}_{zone}_{date_range_str(start, end)}"
    return hashlib.md5(raw.encode()).hexdigest()[:16] + f"_{provider}_{dataset}_{zone}"


def _path(key: str) -> Path:
    return _CACHE_DIR / f"{key}.parquet"


def load(provider: str, dataset: str, zone: str, start, end) -> pd.DataFrame | None:
    """Return cached DataFrame or None if absent / cache unavailable."""
    if not _PARQUET_OK:
        return None
    p = _path(_cache_key(provider, dataset, zone, start, end))
    if not p.exists():
        return None
    try:
        return pd.read_parquet(p)
    except Exception:
        # Corrupt or unreadable file — treat as cache miss, not a crash.
        return None


def save(df: pd.DataFrame, provider: str, dataset: str, zone: str, start, end) -> None:
    """Persist a DataFrame to the cache (no-op if parquet unavailable)."""
    if not _PARQUET_OK:
        _warn_once()
        return
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(_cache_key(provider, dataset, zone, start, end))
    try:
        df.to_parquet(p)
    except Exception:
        pass  # never let a cache write crash the caller


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
