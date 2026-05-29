"""Filesystem cache for provider responses.

Parquet is used when available (pyarrow or fastparquet).  If neither is
installed the cache silently no-ops — all ``load()`` calls return ``None``
and ``save()`` emits a one-time ``UserWarning`` so users know caching is off.

Data types and TTL
------------------
*Historical* data (``end`` date is before today UTC) is immutable — it
never changes after the fact and is cached forever.

*Live* data (``end`` is today or in the future) may be updated by the
provider.  It is cached with a configurable TTL (default 1 hour) and
automatically re-fetched when the entry expires.

Metadata sidecar
----------------
Each ``{key}.parquet`` has a companion ``{key}.meta.json`` that records:

.. code-block:: json

    {
        "cached_at": "2025-01-15T10:30:00+00:00",
        "is_complete": true,
        "provider": "smard",
        "dataset": "prices",
        "zone": "DE_LU"
    }

Cache directory
---------------
Default: ``~/.clarigrid/cache/``.  Override with the
``CLARIGRID_CACHE_DIR`` environment variable.

Install pyarrow to enable: ``pip install pyarrow``
"""

from __future__ import annotations

import hashlib
import json
import os
import warnings
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from clarigrid.utils.time import date_range_str

# ── Default cache location ─────────────────────────────────────────────────

_DEFAULT_CACHE_DIR = Path(
    os.environ.get("CLARIGRID_CACHE_DIR", Path.home() / ".clarigrid" / "cache")
)

# ── Parquet engine detection ───────────────────────────────────────────────

_PARQUET_OK: bool = False
try:
    import pyarrow  # noqa: F401
    _PARQUET_OK = True
except ImportError:
    try:
        import fastparquet  # noqa: F401
        _PARQUET_OK = True
    except ImportError:
        pass


# ── Metadata dataclass ─────────────────────────────────────────────────────

@dataclass
class _EntryMeta:
    cached_at: str      # ISO 8601 with UTC offset
    is_complete: bool   # True = historical (immutable); False = live (TTL applies)
    provider: str
    dataset: str
    zone: str
    currency: str | None = None  # ISO currency code for price datasets ("EUR"/"GBP")

    @classmethod
    def from_file(cls, path: Path) -> "_EntryMeta":
        with open(path) as f:
            d = json.load(f)
        return cls(**d)

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2))

    def is_expired(self, ttl_seconds: int) -> bool:
        """True if this is a live entry whose TTL has elapsed."""
        if self.is_complete:
            return False
        try:
            cached = datetime.fromisoformat(self.cached_at)
            age = (datetime.now(timezone.utc) - cached).total_seconds()
            return age > ttl_seconds
        except Exception:
            return True  # corrupt metadata → treat as expired


# ── Helper ─────────────────────────────────────────────────────────────────

def _is_historical(end: Any) -> bool:
    """Return True if *end* is strictly before today UTC (data is immutable).

    Uses UTC for both sides of the comparison — avoids false positives near
    midnight when the local system timezone is ahead of UTC.
    """
    end_date = pd.Timestamp(end).tz_localize("UTC").date()
    today_utc = pd.Timestamp.now(tz="UTC").date()
    return end_date < today_utc


# ── CacheManager ───────────────────────────────────────────────────────────

class CacheManager:
    """Filesystem Parquet cache with TTL-aware expiry for live data.

    Args:
        cache_dir: Directory for cache files.  Defaults to
            ``~/.clarigrid/cache/`` (or ``$CLARIGRID_CACHE_DIR``).
        live_ttl_seconds: How long live / partial data entries remain
            valid before triggering a refetch.  Default is 3600 (1 hour).
            Historical data (end date before today) never expires.
    """

    def __init__(
        self,
        cache_dir: Path | str | None = None,
        live_ttl_seconds: int = 3600,
    ) -> None:
        self._dir = Path(cache_dir) if cache_dir else _DEFAULT_CACHE_DIR
        self._live_ttl = live_ttl_seconds
        self._warned = False

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_live_ttl(self, seconds: int) -> None:
        """Change the TTL for live data entries.

        Args:
            seconds: Seconds before a live cache entry is considered stale
                and a network refetch is triggered.  Set to 0 to always
                refetch live data.
        """
        self._live_ttl = seconds

    @property
    def live_ttl(self) -> int:
        """Current live-data TTL in seconds."""
        return self._live_ttl

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def load(
        self,
        provider: str,
        dataset: str,
        zone: str,
        start: Any,
        end: Any,
    ) -> pd.DataFrame | None:
        """Return cached DataFrame, or ``None`` on cache miss / expiry.

        Returns ``None`` when:
        - Parquet engine not installed.
        - No cache file exists for this key.
        - Entry is live data and the TTL has elapsed.
        - Cache file is corrupt or unreadable.
        """
        if not _PARQUET_OK:
            return None
        key = self._cache_key(provider, dataset, zone, start, end)
        parquet_path = self._parquet_path(key)
        meta_path = self._meta_path(key)

        if not parquet_path.exists():
            return None

        # Check TTL via metadata sidecar.
        if meta_path.exists():
            try:
                meta = _EntryMeta.from_file(meta_path)
                if meta.is_expired(self._live_ttl):
                    return None
            except Exception:
                return None  # corrupt metadata → cache miss

        try:
            df = pd.read_parquet(parquet_path)
            # Restore currency to attrs so normalise_prices() picks it up on cache hit.
            if meta_path.exists():
                try:
                    m = _EntryMeta.from_file(meta_path)
                    if m.currency:
                        df.attrs["currency"] = m.currency
                except Exception:
                    pass
            return df
        except Exception:
            return None

    def save(
        self,
        df: pd.DataFrame,
        provider: str,
        dataset: str,
        zone: str,
        start: Any,
        end: Any,
    ) -> None:
        """Persist *df* to the cache (no-op if parquet unavailable).

        Writes a metadata sidecar alongside the Parquet file so TTL
        expiry and introspection work correctly.
        """
        if not _PARQUET_OK:
            self._warn_once()
            return
        if df.empty:
            return

        self._dir.mkdir(parents=True, exist_ok=True)
        key = self._cache_key(provider, dataset, zone, start, end)
        parquet_path = self._parquet_path(key)
        meta_path = self._meta_path(key)

        try:
            df.to_parquet(parquet_path)
            meta = _EntryMeta(
                cached_at=datetime.now(timezone.utc).isoformat(),
                is_complete=_is_historical(end),
                provider=provider,
                dataset=dataset,
                zone=zone,
                currency=df.attrs.get("currency"),
            )
            meta.write(meta_path)
        except Exception:
            pass  # never crash the caller on a cache write failure

    def clear(self, provider: str | None = None) -> int:
        """Delete cache files.

        Args:
            provider: If given, only delete entries for that provider.
                If ``None``, clear the entire cache.

        Returns:
            Number of Parquet files removed.
        """
        if not self._dir.exists():
            return 0
        removed = 0
        for parquet_file in self._dir.glob("*.parquet"):
            if provider is None or f"_{provider}_" in parquet_file.name:
                parquet_file.unlink(missing_ok=True)
                # Remove companion metadata.
                meta = parquet_file.with_suffix(".meta.json")
                meta.unlink(missing_ok=True)
                removed += 1
        return removed

    def info(self) -> pd.DataFrame:
        """Return a DataFrame listing all cached entries with metadata.

        Columns: ``provider``, ``dataset``, ``zone``, ``cached_at``,
        ``type`` (``"historical"`` or ``"live"``), ``expired``,
        ``size_kb``.

        Example::

            import clarigrid as cg
            print(cg.cache.info())
        """
        if not self._dir.exists():
            return pd.DataFrame()
        rows = []
        for meta_file in self._dir.glob("*.meta.json"):
            try:
                meta = _EntryMeta.from_file(meta_file)
                parquet_file = meta_file.with_suffix("").with_suffix(".parquet")
                size_kb = (
                    parquet_file.stat().st_size // 1024
                    if parquet_file.exists()
                    else 0
                )
                rows.append(
                    {
                        "provider": meta.provider,
                        "dataset": meta.dataset,
                        "zone": meta.zone,
                        "cached_at": meta.cached_at,
                        "type": "historical" if meta.is_complete else "live",
                        "expired": meta.is_expired(self._live_ttl),
                        "size_kb": size_kb,
                    }
                )
            except Exception:
                continue
        if not rows:
            return pd.DataFrame(
                columns=["provider", "dataset", "zone", "cached_at", "type", "expired", "size_kb"]
            )
        return (
            pd.DataFrame(rows)
            .sort_values(["provider", "dataset", "zone"])
            .reset_index(drop=True)
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cache_key(
        self, provider: str, dataset: str, zone: str, start: Any, end: Any
    ) -> str:
        raw = f"{provider}_{dataset}_{zone}_{date_range_str(start, end)}"
        short = hashlib.md5(raw.encode()).hexdigest()[:16]
        # Human-readable suffix makes manual inspection easier.
        safe = zone.replace("/", "_").replace("\\", "_")
        return f"{short}_{provider}_{dataset}_{safe}"

    def _parquet_path(self, key: str) -> Path:
        return self._dir / f"{key}.parquet"

    def _meta_path(self, key: str) -> Path:
        return self._dir / f"{key}.meta.json"

    def _warn_once(self) -> None:
        if not self._warned:
            warnings.warn(
                "Clarigrid cache disabled: neither pyarrow nor fastparquet is installed. "
                "Queries will always hit the network. "
                "To enable caching run:  pip install pyarrow",
                UserWarning,
                stacklevel=4,
            )
            self._warned = True


# ── Module-level singleton and convenience functions ───────────────────────
# These allow both ``from clarigrid.core import cache; cache.load(...)``
# and ``import clarigrid as cg; cg.cache.info()``.

_manager: CacheManager = CacheManager()


def load(provider: str, dataset: str, zone: str, start: Any, end: Any) -> pd.DataFrame | None:
    return _manager.load(provider, dataset, zone, start, end)


def save(df: pd.DataFrame, provider: str, dataset: str, zone: str, start: Any, end: Any) -> None:
    _manager.save(df, provider, dataset, zone, start, end)


def clear(provider: str | None = None) -> int:
    return _manager.clear(provider)


def info() -> pd.DataFrame:
    """List all cached entries with metadata.  Delegates to the default manager."""
    return _manager.info()


def set_live_ttl(seconds: int) -> None:
    """Set the TTL for live data entries on the default cache manager."""
    _manager.set_live_ttl(seconds)


def _cache_key(provider: str, dataset: str, zone: str, start: Any, end: Any) -> str:
    """Return the cache key string for a given request (delegates to default manager)."""
    return _manager._cache_key(provider, dataset, zone, start, end)
