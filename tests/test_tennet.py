"""Integration tests — TenneT NL provider (developer.tennet.eu).

TenneT requires a free API key. Tests are split into two groups:

1. **Key-independent** (always run): registration, stub behaviour when no key
   is configured, capabilities, NotImplementedError for generation.

2. **Key-required** (skipped if ``CLARIGRID_TENNET_API_KEY`` / keys.toml entry
   is absent): live data tests for settlement prices and metered injections.

To run live tests:
    clarigrid keys set tennet YOUR_KEY   # or export CLARIGRID_TENNET_API_KEY=...
    pytest tests/test_tennet.py -m live
"""

from __future__ import annotations

import pandas as pd
import pytest
from unittest.mock import patch

import clarigrid as cg
import clarigrid.core.cache as _cache
from clarigrid.core import http as _http
from clarigrid.core import config as _config

pytestmark = pytest.mark.live

# Detect key availability at module load time so skip decorators work.
HAS_KEY = bool(_config.get_api_key("tennet"))

START = "2026-02-01"
END = "2026-02-03"
ZONE = "NL"
PROVIDER = "tennet"


@pytest.fixture(autouse=True)
def clear_cache():
    _cache.clear(PROVIDER)
    yield
    _cache.clear(PROVIDER)


def _cache_file_exists() -> bool:
    if not _cache._CACHE_DIR.exists():
        return False
    return any(f"_{PROVIDER}_" in f.name for f in _cache._CACHE_DIR.glob("*.parquet"))


# ---------------------------------------------------------------------------
# Registration & connection (no key required)
# ---------------------------------------------------------------------------


def test_tennet_registered():
    """TenneT must register regardless of whether a key is configured."""
    assert PROVIDER in cg.list_providers()


def test_tennet_connect():
    """cg.connect('tennet') must not raise even without a key (stub registered)."""
    cg.connect(PROVIDER)


def test_tennet_provider_name():
    from clarigrid.core.registry import get_provider
    p = get_provider(PROVIDER)
    assert "tennet" in p.name().lower() or "TenneT" in p.name()


# ---------------------------------------------------------------------------
# Stub behaviour (no key configured)
# ---------------------------------------------------------------------------


def test_tennet_stub_raises_on_get_prices():
    """When no API key is configured, get_prices must raise RuntimeError."""
    if HAS_KEY:
        pytest.skip("API key is configured — stub is not active")
    from clarigrid.core.registry import get_provider
    p = get_provider(PROVIDER)
    with pytest.raises(RuntimeError, match="[Kk]ey|key|register|developer.tennet"):
        p.get_prices(ZONE, START, END)


def test_tennet_stub_raises_on_get_load():
    if HAS_KEY:
        pytest.skip("API key is configured — stub is not active")
    from clarigrid.core.registry import get_provider
    p = get_provider(PROVIDER)
    with pytest.raises(RuntimeError):
        p.get_load(ZONE, START, END)


def test_tennet_stub_capabilities_empty():
    """Stub provider must advertise no capabilities."""
    if HAS_KEY:
        pytest.skip("API key is configured — stub is not active")
    from clarigrid.core.registry import get_provider
    p = get_provider(PROVIDER)
    assert p.capabilities() == set()


# ---------------------------------------------------------------------------
# Generation not supported (always — regardless of key)
# ---------------------------------------------------------------------------


def test_tennet_generation_not_supported():
    """TenneT NL has no generation mix endpoint; must raise NotImplementedError."""
    from clarigrid.providers.tennet import TennetProvider, _TennetStub
    from clarigrid.core.registry import get_provider

    p = get_provider(PROVIDER)
    with pytest.raises((NotImplementedError, RuntimeError)):
        # Stub raises RuntimeError (no key); live provider raises NotImplementedError.
        p.get_generation(ZONE, START, END)


# ---------------------------------------------------------------------------
# Live tests (skipped when no API key is present)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_KEY, reason="CLARIGRID_TENNET_API_KEY not configured")
def test_tennet_prices_returns_df():
    """Settlement prices must return a non-empty UTC DataFrame."""
    df = cg.get_prices(ZONE, START, END, source=PROVIDER, use_cache=False)
    assert not df.empty
    assert isinstance(df.index, pd.DatetimeIndex)
    assert str(df.index.tz) == "UTC"


@pytest.mark.skipif(not HAS_KEY, reason="CLARIGRID_TENNET_API_KEY not configured")
def test_tennet_prices_columns():
    """Settlement prices must contain up- and down-regulation price columns."""
    df = cg.get_prices(ZONE, START, END, source=PROVIDER, use_cache=False)
    assert "up_regulation_price_eur_mwh" in df.columns, (
        f"Missing up_regulation_price_eur_mwh. Got: {sorted(df.columns)}"
    )
    assert "down_regulation_price_eur_mwh" in df.columns, (
        f"Missing down_regulation_price_eur_mwh. Got: {sorted(df.columns)}"
    )


@pytest.mark.skipif(not HAS_KEY, reason="CLARIGRID_TENNET_API_KEY not configured")
def test_tennet_prices_15min_resolution():
    """TenneT uses 15-minute PTUs; median interval must equal 15 min."""
    df = cg.get_prices(ZONE, START, END, source=PROVIDER, use_cache=False)
    assert len(df) >= 192, f"Expected ≥192 rows (15-min × 2 days), got {len(df)}"
    med = df.index.to_series().diff().dropna().median()
    assert med == pd.Timedelta("15min"), f"Expected 15-min PTU intervals, got {med}"


@pytest.mark.skipif(not HAS_KEY, reason="CLARIGRID_TENNET_API_KEY not configured")
def test_tennet_prices_numeric():
    """Up- and down-regulation prices must be numeric (float)."""
    df = cg.get_prices(ZONE, START, END, source=PROVIDER, use_cache=False)
    for col in ["up_regulation_price_eur_mwh", "down_regulation_price_eur_mwh"]:
        assert pd.api.types.is_numeric_dtype(df[col]), (
            f"Column '{col}' not numeric: {df[col].dtype}"
        )


@pytest.mark.skipif(not HAS_KEY, reason="CLARIGRID_TENNET_API_KEY not configured")
def test_tennet_load_returns_df():
    """Metered injections must return a non-empty UTC DataFrame."""
    df = cg.get_load(ZONE, START, END, source=PROVIDER, use_cache=False)
    assert not df.empty
    assert str(df.index.tz) == "UTC"
    assert "load_mw" in df.columns, f"Missing load_mw. Got: {sorted(df.columns)}"


@pytest.mark.skipif(not HAS_KEY, reason="CLARIGRID_TENNET_API_KEY not configured")
def test_tennet_load_15min_resolution():
    df = cg.get_load(ZONE, START, END, source=PROVIDER, use_cache=False)
    assert len(df) >= 192
    med = df.index.to_series().diff().dropna().median()
    assert med == pd.Timedelta("15min")


# ---------------------------------------------------------------------------
# Caching (live — key required)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_KEY, reason="CLARIGRID_TENNET_API_KEY not configured")
@pytest.mark.skipif(not _cache._PARQUET_OK, reason="pyarrow not installed")
def test_tennet_use_cache_false_no_file_written():
    df = cg.get_prices(ZONE, START, END, source=PROVIDER, use_cache=False)
    assert not df.empty
    assert not _cache_file_exists(), "use_cache=False must not write cache"


@pytest.mark.skipif(not HAS_KEY, reason="CLARIGRID_TENNET_API_KEY not configured")
@pytest.mark.skipif(not _cache._PARQUET_OK, reason="pyarrow not installed")
def test_tennet_cache_miss_then_hit():
    assert not _cache_file_exists()

    df1 = cg.get_prices(ZONE, START, END, source=PROVIDER, use_cache=True)
    assert not df1.empty
    assert _cache_file_exists()

    with patch.object(_http._SESSION, "get", side_effect=AssertionError("network hit on cache hit")):
        df2 = cg.get_prices(ZONE, START, END, source=PROVIDER, use_cache=True)

    assert not df2.empty
    pd.testing.assert_frame_equal(df1, df2)


@pytest.mark.skipif(not HAS_KEY, reason="CLARIGRID_TENNET_API_KEY not configured")
@pytest.mark.skipif(not _cache._PARQUET_OK, reason="pyarrow not installed")
def test_tennet_cache_cleared_forces_network():
    df1 = cg.get_prices(ZONE, START, END, source=PROVIDER, use_cache=True)
    assert _cache_file_exists()

    removed = _cache.clear(PROVIDER)
    assert removed >= 1
    assert not _cache_file_exists()

    df2 = cg.get_prices(ZONE, START, END, source=PROVIDER, use_cache=True)
    assert not df2.empty
    pd.testing.assert_frame_equal(df1, df2)
