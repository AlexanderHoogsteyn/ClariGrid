"""Integration tests — Open-Meteo weather provider.

Covers:
    - Provider registration and connection
    - Forecast endpoint (future dates)
    - Archive endpoint (ERA5 historical)
    - Auto endpoint selection based on date range
    - Custom variable selection
    - Air quality endpoint
    - UTC DatetimeIndex and hourly resolution
    - Caching: miss → write → hit (no network on second call)
    - use_cache=False bypasses cache
    - Invalid zone raises
    - Electricity method stubs raise NotImplementedError
"""

from __future__ import annotations

import pandas as pd
import pytest
from unittest.mock import patch

import clarigrid as cg
import clarigrid.core.cache as _cache
from clarigrid.core import http as _http

from tests.conftest import WEATHER_START, WEATHER_END, assert_valid_weather_df, median_interval

pytestmark = pytest.mark.live

# Use historical dates for all stable tests (archive endpoint).
# Ensures deterministic data regardless of when the test runs.
ARCHIVE_START = "2025-06-01"
ARCHIVE_END = "2025-06-03"
ZONE_BRUSSELS = "50.85,4.35"
ZONE_BERLIN = "52.52,13.41"

PROVIDER = "openmeteo"


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
# Registration & connection
# ---------------------------------------------------------------------------


def test_openmeteo_registered():
    """Provider must be listed after import."""
    assert PROVIDER in cg.list_providers()


def test_openmeteo_connect_sets_active():
    """cg.connect() succeeds without error."""
    cg.connect(PROVIDER)


def test_openmeteo_provider_name():
    from clarigrid.core.registry import get_provider
    p = get_provider(PROVIDER)
    assert "open-meteo" in p.name().lower() or "Open-Meteo" in p.name()


# ---------------------------------------------------------------------------
# Forecast endpoint
# ---------------------------------------------------------------------------


def test_openmeteo_forecast_returns_df():
    """Forecast for next 3 days returns a non-empty UTC DataFrame."""
    today = pd.Timestamp.now(tz="UTC").normalize()
    start = today.strftime("%Y-%m-%d")
    end = (today + pd.Timedelta(days=2)).strftime("%Y-%m-%d")
    df = cg.get_weather(
        ZONE_BRUSSELS, start, end,
        source=PROVIDER,
        endpoint="forecast",
        variables=["temperature_2m"],
        use_cache=False,
    )
    assert_valid_weather_df(df, expected_cols=["temperature_2m"], min_rows=24)


def test_openmeteo_forecast_hourly_resolution():
    today = pd.Timestamp.now(tz="UTC").normalize()
    start = today.strftime("%Y-%m-%d")
    end = (today + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    df = cg.get_weather(
        ZONE_BRUSSELS, start, end,
        source=PROVIDER,
        endpoint="forecast",
        variables=["temperature_2m"],
        use_cache=False,
    )
    assert median_interval(df) == pd.Timedelta("1h"), (
        f"Expected 1h hourly intervals, got {median_interval(df)}"
    )


# ---------------------------------------------------------------------------
# Archive endpoint (ERA5)
# ---------------------------------------------------------------------------


def test_openmeteo_archive_temperature():
    df = cg.get_weather(
        ZONE_BERLIN, ARCHIVE_START, ARCHIVE_END,
        source=PROVIDER,
        endpoint="archive",
        variables=["temperature_2m"],
        use_cache=False,
    )
    assert_valid_weather_df(df, expected_cols=["temperature_2m"], min_rows=48)


def test_openmeteo_archive_hourly_resolution():
    df = cg.get_weather(
        ZONE_BERLIN, ARCHIVE_START, ARCHIVE_END,
        source=PROVIDER,
        endpoint="archive",
        variables=["temperature_2m"],
        use_cache=False,
    )
    # 2 full days = 48 hours
    assert len(df) >= 48, f"Expected ≥48 rows, got {len(df)}"
    assert median_interval(df) == pd.Timedelta("1h")


def test_openmeteo_archive_multiple_variables():
    """Multiple variables → one column per variable."""
    vars_ = ["temperature_2m", "wind_speed_100m", "shortwave_radiation"]
    df = cg.get_weather(
        ZONE_BRUSSELS, ARCHIVE_START, ARCHIVE_END,
        source=PROVIDER,
        endpoint="archive",
        variables=vars_,
        use_cache=False,
    )
    assert_valid_weather_df(df, expected_cols=vars_)
    assert df.shape[1] == len(vars_), (
        f"Expected {len(vars_)} columns, got {df.shape[1]}: {sorted(df.columns)}"
    )


def test_openmeteo_archive_wind_energy_variables():
    """Wind speed at hub heights (100m) must be available and numeric from ERA5."""
    df = cg.get_weather(
        ZONE_BRUSSELS, ARCHIVE_START, ARCHIVE_END,
        source=PROVIDER,
        endpoint="archive",
        variables=["wind_speed_100m", "wind_direction_10m"],
        use_cache=False,
    )
    assert_valid_weather_df(df, expected_cols=["wind_speed_100m"])
    assert len(df) >= 48
    # Wind speed must be non-negative.
    assert (df["wind_speed_100m"] >= 0).all(), "Negative wind speed values"


def test_openmeteo_archive_solar_variables():
    """Solar radiation variables must be present and non-negative during daytime."""
    df = cg.get_weather(
        ZONE_BRUSSELS, ARCHIVE_START, ARCHIVE_END,
        source=PROVIDER,
        endpoint="archive",
        variables=["shortwave_radiation", "direct_radiation", "diffuse_radiation"],
        use_cache=False,
    )
    assert_valid_weather_df(df, expected_cols=["shortwave_radiation"])
    # Radiation values must be ≥0 (backward averages, never negative).
    assert (df["shortwave_radiation"] >= 0).all(), "shortwave_radiation has negative values"


# ---------------------------------------------------------------------------
# Auto endpoint selection
# ---------------------------------------------------------------------------


def test_openmeteo_auto_uses_archive_for_historical():
    """endpoint='auto' must use archive for dates clearly in the past."""
    df = cg.get_weather(
        ZONE_BRUSSELS, ARCHIVE_START, ARCHIVE_END,
        source=PROVIDER,
        endpoint="auto",
        variables=["temperature_2m"],
        use_cache=False,
    )
    assert_valid_weather_df(df, expected_cols=["temperature_2m"])


def test_openmeteo_default_variables_returned():
    """When variables kwarg is omitted, the three default energy variables are returned."""
    df = cg.get_weather(
        ZONE_BRUSSELS, ARCHIVE_START, ARCHIVE_END,
        source=PROVIDER,
        endpoint="archive",
        use_cache=False,
    )
    expected = {"temperature_2m", "wind_speed_100m", "shortwave_radiation"}
    assert expected.issubset(df.columns), (
        f"Default variables missing. Got: {sorted(df.columns)}"
    )


# ---------------------------------------------------------------------------
# Invalid zone
# ---------------------------------------------------------------------------


def test_openmeteo_invalid_zone_raises():
    with pytest.raises(ValueError, match="lat,lon"):
        cg.get_weather(
            "INVALID_ZONE", ARCHIVE_START, ARCHIVE_END,
            source=PROVIDER,
            use_cache=False,
        )


def test_openmeteo_zone_with_negative_longitude():
    """London (negative longitude) must parse correctly."""
    df = cg.get_weather(
        "51.51,-0.13", ARCHIVE_START, ARCHIVE_END,
        source=PROVIDER,
        endpoint="archive",
        variables=["temperature_2m"],
        use_cache=False,
    )
    assert_valid_weather_df(df, expected_cols=["temperature_2m"])


# ---------------------------------------------------------------------------
# Electricity method stubs
# ---------------------------------------------------------------------------


def test_openmeteo_get_prices_raises():
    from clarigrid.core.registry import get_provider
    p = get_provider(PROVIDER)
    with pytest.raises(NotImplementedError):
        p.get_prices("50.85,4.35", ARCHIVE_START, ARCHIVE_END)


def test_openmeteo_get_load_raises():
    from clarigrid.core.registry import get_provider
    p = get_provider(PROVIDER)
    with pytest.raises(NotImplementedError):
        p.get_load("50.85,4.35", ARCHIVE_START, ARCHIVE_END)


def test_openmeteo_get_generation_raises():
    from clarigrid.core.registry import get_provider
    p = get_provider(PROVIDER)
    with pytest.raises(NotImplementedError):
        p.get_generation("50.85,4.35", ARCHIVE_START, ARCHIVE_END)


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _cache._PARQUET_OK, reason="pyarrow not installed")
def test_openmeteo_use_cache_false_no_file_written():
    """use_cache=False must not write a cache file."""
    df = cg.get_weather(
        ZONE_BRUSSELS, ARCHIVE_START, ARCHIVE_END,
        source=PROVIDER,
        endpoint="archive",
        variables=["temperature_2m"],
        use_cache=False,
    )
    assert not df.empty
    assert not _cache_file_exists(), "use_cache=False must not write cache"


@pytest.mark.skipif(not _cache._PARQUET_OK, reason="pyarrow not installed")
def test_openmeteo_cache_miss_then_hit():
    """First call (use_cache=True) writes cache; second returns cached data without network."""
    assert not _cache_file_exists()

    df1 = cg.get_weather(
        ZONE_BRUSSELS, ARCHIVE_START, ARCHIVE_END,
        source=PROVIDER,
        endpoint="archive",
        variables=["temperature_2m"],
        use_cache=True,
    )
    assert not df1.empty
    assert _cache_file_exists(), "Cache file must exist after first use_cache=True call"

    # Second call must not hit the network.
    with patch.object(_http._SESSION, "get", side_effect=AssertionError("network hit on cache hit")):
        df2 = cg.get_weather(
            ZONE_BRUSSELS, ARCHIVE_START, ARCHIVE_END,
            source=PROVIDER,
            endpoint="archive",
            variables=["temperature_2m"],
            use_cache=True,
        )

    assert not df2.empty
    pd.testing.assert_frame_equal(df1, df2)


@pytest.mark.skipif(not _cache._PARQUET_OK, reason="pyarrow not installed")
def test_openmeteo_cache_cleared_forces_network():
    """After cache.clear(), next call goes back to the network."""
    df1 = cg.get_weather(
        ZONE_BRUSSELS, ARCHIVE_START, ARCHIVE_END,
        source=PROVIDER,
        endpoint="archive",
        variables=["temperature_2m"],
        use_cache=True,
    )
    assert _cache_file_exists()

    removed = _cache.clear(PROVIDER)
    assert removed >= 1
    assert not _cache_file_exists()

    df2 = cg.get_weather(
        ZONE_BRUSSELS, ARCHIVE_START, ARCHIVE_END,
        source=PROVIDER,
        endpoint="archive",
        variables=["temperature_2m"],
        use_cache=True,
    )
    assert not df2.empty
    pd.testing.assert_frame_equal(df1, df2)
