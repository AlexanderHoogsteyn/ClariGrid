"""Integration tests — RMI Belgium weather provider (opendata.meteo.be).

Covers:
    - Provider registration and connection
    - SYNOP network: temperature, wind, pressure, cloud cover (3-hourly)
    - AWS network: temperature, radiation, wind (10-minute)
    - UTC DatetimeIndex and expected resolutions
    - Caching: miss → write → hit (no network on second call)
    - use_cache=False bypasses cache
    - Electricity method stubs raise NotImplementedError
"""

from __future__ import annotations

import sys
print(sys.executable)

import pandas as pd
import pytest
from unittest.mock import patch

import clarigrid as cg
import clarigrid.core.cache as _cache
from clarigrid.core import http as _http

from tests.conftest import assert_valid_weather_df, median_interval

pytestmark = pytest.mark.live

# Use a stable historical window with known SYNOP data.
# Uccle (code 6447) has continuous data back to the 1980s.
SYNOP_START = "2026-02-01"
SYNOP_END = "2026-02-03"
SYNOP_STATION = "6447"   # Uccle (Brussels)

# AWS Zeebrugge available from 2017-11-18 onwards.
AWS_START = "2026-02-01"
AWS_END = "2026-02-02"
AWS_STATION = "6455"     # Zeebrugge

PROVIDER = "rmi"


@pytest.fixture(autouse=True)
def clear_cache():
    _cache.clear(PROVIDER)
    yield
    _cache.clear(PROVIDER)


def _cache_file_exists() -> bool:
    if not _cache._manager._dir.exists():
        return False
    return any(f"_{PROVIDER}_" in f.name for f in _cache._manager._dir.glob("*.parquet"))


# ---------------------------------------------------------------------------
# Registration & connection
# ---------------------------------------------------------------------------


def test_rmi_registered():
    assert PROVIDER in cg.list_providers()


def test_rmi_connect():
    cg.connect(PROVIDER)


def test_rmi_provider_name():
    from clarigrid.core.registry import get_provider
    p = get_provider(PROVIDER)
    assert "rmi" in p.name().lower() or "Belgium" in p.name()


# ---------------------------------------------------------------------------
# SYNOP — 3-hourly synoptic network
# ---------------------------------------------------------------------------


def test_rmi_synop_returns_df():
    df = cg.get_weather(SYNOP_STATION, SYNOP_START, SYNOP_END,
                        source=PROVIDER, use_cache=False)
    assert_valid_weather_df(df, min_rows=10)


def test_rmi_synop_temperature_column():
    df = cg.get_weather(SYNOP_STATION, SYNOP_START, SYNOP_END,
                        source=PROVIDER, use_cache=False)
    assert "temperature_c" in df.columns, (
        f"Missing temperature_c. Got: {sorted(df.columns)}"
    )
    # Reasonable temperature range for Belgium in February: −20°C to +25°C.
    temps = df["temperature_c"].dropna()
    assert not temps.empty
    assert (temps > -20).all() and (temps < 25).all(), (
        f"Implausible temperatures: min={temps.min():.1f}, max={temps.max():.1f}"
    )


def test_rmi_synop_expected_columns():
    """Key SYNOP observation fields must be present."""
    df = cg.get_weather(SYNOP_STATION, SYNOP_START, SYNOP_END,
                        source=PROVIDER, use_cache=False)
    for col in ["temperature_c", "wind_speed_ms", "humidity_pct", "pressure_hpa"]:
        assert col in df.columns, f"Missing '{col}'. Got: {sorted(df.columns)}"


def test_rmi_synop_resolution():
    """SYNOP observations are at most 3-hourly (some stations report hourly).

    The SYNOP network is nominally 3-hourly but certain stations (including
    Uccle) may report hourly.  Verify that the median interval is between
    30 minutes and 3 hours.
    """
    df = cg.get_weather(SYNOP_STATION, SYNOP_START, SYNOP_END,
                        source=PROVIDER, use_cache=False)
    assert len(df) >= 14, f"Expected ≥14 rows for 2-day SYNOP, got {len(df)}"
    med = median_interval(df)
    assert pd.Timedelta("30min") <= med <= pd.Timedelta("3h"), (
        f"SYNOP interval {med} outside expected 30min–3h range"
    )


def test_rmi_synop_wind_values_plausible():
    df = cg.get_weather(SYNOP_STATION, SYNOP_START, SYNOP_END,
                        source=PROVIDER, use_cache=False)
    wind = df["wind_speed_ms"].dropna()
    assert not wind.empty
    assert (wind >= 0).all(), "Negative wind speed values"
    assert (wind < 100).all(), f"Implausible wind speed: max={wind.max():.1f} m/s"


def test_rmi_synop_utc_index():
    df = cg.get_weather(SYNOP_STATION, SYNOP_START, SYNOP_END,
                        source=PROVIDER, use_cache=False)
    assert str(df.index.tz) == "UTC"


def test_rmi_synop_no_duplicate_timestamps():
    df = cg.get_weather(SYNOP_STATION, SYNOP_START, SYNOP_END,
                        source=PROVIDER, use_cache=False)
    assert not df.index.duplicated().any(), "Duplicate timestamps in SYNOP output"


# ---------------------------------------------------------------------------
# AWS — 10-minute automatic weather stations
# ---------------------------------------------------------------------------


def test_rmi_aws_returns_df():
    df = cg.get_weather(AWS_STATION, AWS_START, AWS_END,
                        source=PROVIDER, dataset="aws", use_cache=False)
    assert_valid_weather_df(df, min_rows=100)


def test_rmi_aws_expected_columns():
    df = cg.get_weather(AWS_STATION, AWS_START, AWS_END,
                        source=PROVIDER, dataset="aws", use_cache=False)
    for col in ["temperature_c", "wind_speed_ms", "humidity_pct"]:
        assert col in df.columns, f"Missing '{col}'. Got: {sorted(df.columns)}"


def test_rmi_aws_10min_resolution():
    """AWS data is 10-minute; median interval must equal 10 min."""
    df = cg.get_weather(AWS_STATION, AWS_START, AWS_END,
                        source=PROVIDER, dataset="aws", use_cache=False)
    # 1 full day × 144 obs/day (10 min).
    assert len(df) >= 130, f"Expected ≥130 rows for 1-day AWS, got {len(df)}"
    med = median_interval(df)
    assert med == pd.Timedelta("10min"), f"Expected 10min AWS intervals, got {med}"


def test_rmi_aws_solar_radiation_present():
    df = cg.get_weather(AWS_STATION, AWS_START, AWS_END,
                        source=PROVIDER, dataset="aws", use_cache=False)
    assert "shortwave_radiation_w_m2" in df.columns, (
        f"Missing shortwave_radiation_w_m2. Got: {sorted(df.columns)}"
    )
    rad = df["shortwave_radiation_w_m2"].dropna()
    assert (rad >= 0).all(), "Negative shortwave radiation"


def test_rmi_aws_utc_index():
    df = cg.get_weather(AWS_STATION, AWS_START, AWS_END,
                        source=PROVIDER, dataset="aws", use_cache=False)
    assert str(df.index.tz) == "UTC"


# ---------------------------------------------------------------------------
# Electricity method stubs
# ---------------------------------------------------------------------------


def test_rmi_get_prices_raises():
    from clarigrid.core.registry import get_provider
    p = get_provider(PROVIDER)
    with pytest.raises(NotImplementedError):
        p.get_prices(SYNOP_STATION, SYNOP_START, SYNOP_END)


def test_rmi_get_load_raises():
    from clarigrid.core.registry import get_provider
    p = get_provider(PROVIDER)
    with pytest.raises(NotImplementedError):
        p.get_load(SYNOP_STATION, SYNOP_START, SYNOP_END)


def test_rmi_get_generation_raises():
    from clarigrid.core.registry import get_provider
    p = get_provider(PROVIDER)
    with pytest.raises(NotImplementedError):
        p.get_generation(SYNOP_STATION, SYNOP_START, SYNOP_END)


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _cache._PARQUET_OK, reason="pyarrow not installed")
def test_rmi_use_cache_false_no_file_written():
    df = cg.get_weather(SYNOP_STATION, SYNOP_START, SYNOP_END,
                        source=PROVIDER, use_cache=False)
    assert not df.empty
    assert not _cache_file_exists(), "use_cache=False must not write cache"


@pytest.mark.skipif(not _cache._PARQUET_OK, reason="pyarrow not installed")
def test_rmi_cache_miss_then_hit():
    """First call writes cache; second returns cached data without network."""
    assert not _cache_file_exists()

    df1 = cg.get_weather(SYNOP_STATION, SYNOP_START, SYNOP_END,
                         source=PROVIDER, use_cache=True)
    assert not df1.empty
    assert _cache_file_exists()

    with patch.object(_http._SESSION, "get", side_effect=AssertionError("network hit on cache hit")):
        df2 = cg.get_weather(SYNOP_STATION, SYNOP_START, SYNOP_END,
                              source=PROVIDER, use_cache=True)

    assert not df2.empty
    pd.testing.assert_frame_equal(df1, df2)


@pytest.mark.skipif(not _cache._PARQUET_OK, reason="pyarrow not installed")
def test_rmi_cache_cleared_forces_network():
    df1 = cg.get_weather(SYNOP_STATION, SYNOP_START, SYNOP_END,
                         source=PROVIDER, use_cache=True)
    assert _cache_file_exists()

    removed = _cache.clear(PROVIDER)
    assert removed >= 1
    assert not _cache_file_exists()

    df2 = cg.get_weather(SYNOP_STATION, SYNOP_START, SYNOP_END,
                         source=PROVIDER, use_cache=True)
    assert not df2.empty
    pd.testing.assert_frame_equal(df1, df2)
