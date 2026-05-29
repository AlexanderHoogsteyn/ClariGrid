"""Tests for the filesystem cache layer.

Covers:
- Cache miss → network fetch → cache write → cache hit on second call.
- Cache cleared → next call goes back to network.
- Cache disabled gracefully when pyarrow is absent (monkeypatching _PARQUET_OK).
- Cache key uniqueness: different providers / zones / date ranges → different files.
"""

from __future__ import annotations

import pandas as pd
import pytest

import clarigrid as cg
import clarigrid.core.cache as _cache

# Use a single fixed date window for all cache tests to minimise network calls.
START = "2025-01-06"
END   = "2025-01-07"
PROVIDER = "smard"
ZONE = "DE"


@pytest.fixture(autouse=True)
def clear_smard_cache():
    """Wipe SMARD cache entries before and after each test to ensure isolation."""
    _cache.clear(PROVIDER)
    yield
    _cache.clear(PROVIDER)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cache_file_exists() -> bool:
    """Return True if at least one SMARD cache parquet file exists."""
    if not _cache._manager._dir.exists():
        return False
    return any(
        f"_{PROVIDER}_" in f.name
        for f in _cache._manager._dir.glob("*.parquet")
    )


# ---------------------------------------------------------------------------
# Basic read-through caching (requires pyarrow)
# ---------------------------------------------------------------------------

@pytest.mark.live
@pytest.mark.skipif(not _cache._PARQUET_OK, reason="pyarrow not installed — cache disabled")
def test_cache_miss_then_hit():
    """First call fetches from network and writes cache; second returns cached data."""
    # 1. Cold cache — no file yet.
    assert not _cache_file_exists(), "Cache should be empty before test"

    # 2. First call: cache miss → network fetch.
    df1 = cg.get_prices(ZONE, START, END, source=PROVIDER, use_cache=True)
    assert not df1.empty

    # 3. Cache file must now exist.
    assert _cache_file_exists(), "Cache file should have been written after first call"

    # 4. Second call: cache hit — no network required.
    #    We verify by patching the HTTP session to raise if used.
    from unittest.mock import patch
    from clarigrid.core import http as _http

    with patch.object(_http._SESSION, "get", side_effect=AssertionError("network hit on second call")):
        df2 = cg.get_prices(ZONE, START, END, source=PROVIDER, use_cache=True)

    assert not df2.empty
    pd.testing.assert_frame_equal(df1, df2)


@pytest.mark.live
@pytest.mark.skipif(not _cache._PARQUET_OK, reason="pyarrow not installed — cache disabled")
def test_cache_cleared_forces_network_fetch():
    """After cache.clear(), the next call must go back to the network."""
    # Prime the cache.
    df1 = cg.get_prices(ZONE, START, END, source=PROVIDER, use_cache=True)
    assert _cache_file_exists(), "Cache should be populated after first call"

    # Clear it.
    removed = _cache.clear(PROVIDER)
    assert removed >= 1, f"Expected ≥1 file removed, got {removed}"
    assert not _cache_file_exists(), "Cache should be empty after clear()"

    # Second call must hit network again and return valid data.
    df2 = cg.get_prices(ZONE, START, END, source=PROVIDER, use_cache=True)
    assert not df2.empty
    pd.testing.assert_frame_equal(df1, df2)


@pytest.mark.live
@pytest.mark.skipif(not _cache._PARQUET_OK, reason="pyarrow not installed — cache disabled")
def test_use_cache_false_bypasses_cache():
    """use_cache=False always hits the network and does not write a cache file."""
    df = cg.get_prices(ZONE, START, END, source=PROVIDER, use_cache=False)
    assert not df.empty
    assert not _cache_file_exists(), "use_cache=False must not write a cache file"


# ---------------------------------------------------------------------------
# Graceful degradation — no pyarrow
# ---------------------------------------------------------------------------

def test_cache_disabled_no_crash(monkeypatch, recwarn):
    """When _PARQUET_OK is False, load() returns None and save() emits a warning
    without crashing."""
    monkeypatch.setattr(_cache, "_PARQUET_OK", False)
    monkeypatch.setattr(_cache._manager, "_warned", False)

    # load() must return None gracefully.
    result = _cache.load(PROVIDER, "prices", ZONE, START, END)
    assert result is None

    df = pd.DataFrame({"price_eur_mwh": [100.0]},
                      index=pd.DatetimeIndex(["2025-01-06 00:00:00+00:00"], name="utc_time"))

    # First save() must emit exactly one UserWarning.
    with pytest.warns(UserWarning, match="cache disabled"):
        _cache.save(df, PROVIDER, "prices", ZONE, START, END)

    # Second save() must NOT emit a second warning (one-time only).
    recwarn.clear()
    _cache.save(df, PROVIDER, "prices", ZONE, START, END)
    user_warnings = [w for w in recwarn.list if issubclass(w.category, UserWarning)]
    assert len(user_warnings) == 0, "save() should only warn once, not on every call"


# ---------------------------------------------------------------------------
# Cache key uniqueness
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _cache._PARQUET_OK, reason="pyarrow not installed — cache disabled")
def test_cache_key_differs_by_zone():
    key_de = _cache._cache_key(PROVIDER, "prices", "DE", START, END)
    key_fr = _cache._cache_key(PROVIDER, "prices", "FR", START, END)
    assert key_de != key_fr


@pytest.mark.skipif(not _cache._PARQUET_OK, reason="pyarrow not installed — cache disabled")
def test_cache_key_differs_by_provider():
    key_smard  = _cache._cache_key("smard",  "prices", ZONE, START, END)
    key_elexon = _cache._cache_key("elexon", "prices", ZONE, START, END)
    assert key_smard != key_elexon


@pytest.mark.skipif(not _cache._PARQUET_OK, reason="pyarrow not installed — cache disabled")
def test_cache_key_differs_by_date_range():
    key1 = _cache._cache_key(PROVIDER, "prices", ZONE, "2025-01-06", "2025-01-07")
    key2 = _cache._cache_key(PROVIDER, "prices", ZONE, "2025-01-07", "2025-01-08")
    assert key1 != key2
