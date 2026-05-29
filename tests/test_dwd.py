"""Integration tests — DWD Open Data / CDC (Germany weather stations).

Covers:
    - Provider registration and connection
    - air_temperature: temperature (°C) and humidity (%)
    - wind: speed (m/s) and direction (°)
    - solar: global radiation (J/cm²)
    - precipitation: hourly amount (mm)
    - Hourly resolution (48 h per 2-day query)
    - Station list helper
    - unknown parameter raises ValueError
    - Caching: miss → write → hit (no network on second call)
    - use_cache=False bypasses cache
    - Electricity method stubs raise NotImplementedError
"""

from __future__ import annotations

import pandas as pd
import pytest
from unittest.mock import patch

import clarigrid as cg
import clarigrid.core.cache as _cache
from clarigrid.core import http as _http

from tests.conftest import assert_valid_weather_df, median_interval

pytestmark = pytest.mark.live

# Berlin-Tempelhof (02564) — long continuous record, confirmed in DWD recent.
# Hamburg (01975) as secondary station for wind tests.
STATION_BERLIN = "02564"
STATION_HAMBURG = "01975"

# Stay within DWD recent/ window (~last 500 days from May 2026).
START = "2026-02-01"
END = "2026-02-03"

PROVIDER = "dwd"


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


def test_dwd_registered():
    assert PROVIDER in cg.list_providers()


def test_dwd_connect():
    cg.connect(PROVIDER)


def test_dwd_provider_name():
    from clarigrid.core.registry import get_provider
    p = get_provider(PROVIDER)
    assert "dwd" in p.name().lower() or "DWD" in p.name()


# ---------------------------------------------------------------------------
# air_temperature
# ---------------------------------------------------------------------------


def test_dwd_air_temperature_returns_df():
    df = cg.get_weather(STATION_BERLIN, START, END,
                        source=PROVIDER, parameter="air_temperature", use_cache=False)
    assert_valid_weather_df(df, expected_cols=["temperature_c", "humidity_pct"])


def test_dwd_air_temperature_hourly_resolution():
    df = cg.get_weather(STATION_BERLIN, START, END,
                        source=PROVIDER, parameter="air_temperature", use_cache=False)
    # 2 full days × 24 h = 48 rows.
    assert len(df) >= 46, f"Expected ≥46 hourly rows, got {len(df)}"
    assert median_interval(df) == pd.Timedelta("1h"), (
        f"Expected hourly intervals, got {median_interval(df)}"
    )


def test_dwd_temperature_values_plausible():
    df = cg.get_weather(STATION_BERLIN, START, END,
                        source=PROVIDER, parameter="air_temperature", use_cache=False)
    temps = df["temperature_c"].dropna()
    assert not temps.empty
    # Berlin February: −25°C to +20°C is a realistic range.
    assert (temps > -25).all() and (temps < 20).all(), (
        f"Implausible Berlin temps: min={temps.min():.1f}, max={temps.max():.1f}"
    )


def test_dwd_humidity_pct_range():
    df = cg.get_weather(STATION_BERLIN, START, END,
                        source=PROVIDER, parameter="air_temperature", use_cache=False)
    hum = df["humidity_pct"].dropna()
    assert not hum.empty
    assert (hum >= 0).all() and (hum <= 100).all(), (
        f"Humidity out of 0–100 range: min={hum.min()}, max={hum.max()}"
    )


def test_dwd_air_temperature_utc_index():
    df = cg.get_weather(STATION_BERLIN, START, END,
                        source=PROVIDER, parameter="air_temperature", use_cache=False)
    assert str(df.index.tz) == "UTC"


# ---------------------------------------------------------------------------
# wind
# ---------------------------------------------------------------------------


def test_dwd_wind_returns_df():
    df = cg.get_weather(STATION_BERLIN, START, END,
                        source=PROVIDER, parameter="wind", use_cache=False)
    assert_valid_weather_df(df, expected_cols=["wind_speed_ms", "wind_direction_deg"])


def test_dwd_wind_speed_non_negative():
    df = cg.get_weather(STATION_BERLIN, START, END,
                        source=PROVIDER, parameter="wind", use_cache=False)
    speed = df["wind_speed_ms"].dropna()
    assert not speed.empty
    assert (speed >= 0).all(), f"Negative wind speeds: {speed[speed < 0].values}"


def test_dwd_wind_direction_range():
    df = cg.get_weather(STATION_BERLIN, START, END,
                        source=PROVIDER, parameter="wind", use_cache=False)
    direction = df["wind_direction_deg"].dropna()
    assert not direction.empty
    # DWD uses 0–360 for direction; calm wind = 0.
    assert (direction >= 0).all() and (direction <= 360).all(), (
        f"Wind direction out of 0–360 range: min={direction.min()}, max={direction.max()}"
    )


# ---------------------------------------------------------------------------
# solar
# ---------------------------------------------------------------------------

# DWD solar data lives directly in hourly/solar/ (no recent/historical subdir).
# Files use the _row.zip suffix. Use station 00183 (Arkona, known solar record).
STATION_SOLAR = "00183"


def test_dwd_solar_returns_df():
    df = cg.get_weather(STATION_SOLAR, START, END,
                        source=PROVIDER, parameter="solar", use_cache=False)
    assert not df.empty
    assert_valid_weather_df(df)
    assert len(df) >= 46


def test_dwd_solar_radiation_non_negative():
    df = cg.get_weather(STATION_SOLAR, START, END,
                        source=PROVIDER, parameter="solar", use_cache=False)
    if "global_radiation_j_cm2" in df.columns:
        rad = df["global_radiation_j_cm2"].dropna()
        assert (rad >= 0).all(), "Negative solar radiation values"


# ---------------------------------------------------------------------------
# precipitation
# ---------------------------------------------------------------------------


def test_dwd_precipitation_returns_df():
    df = cg.get_weather(STATION_BERLIN, START, END,
                        source=PROVIDER, parameter="precipitation", use_cache=False)
    assert_valid_weather_df(df, min_rows=46)
    assert "precipitation_mm" in df.columns, (
        f"Missing precipitation_mm. Got: {sorted(df.columns)}"
    )


def test_dwd_precipitation_non_negative():
    df = cg.get_weather(STATION_BERLIN, START, END,
                        source=PROVIDER, parameter="precipitation", use_cache=False)
    rain = df["precipitation_mm"].dropna()
    assert (rain >= 0).all(), "Negative precipitation values"


# ---------------------------------------------------------------------------
# Station list
# ---------------------------------------------------------------------------


def test_dwd_station_list_returns_df():
    from clarigrid.providers.dwd import dwd_station_list
    stations = dwd_station_list("air_temperature")
    assert not stations.empty
    assert "station_id" in stations.columns
    assert "lat" in stations.columns
    assert "lon" in stations.columns
    # Berlin-Tempelhof must appear.
    assert STATION_BERLIN in stations["station_id"].values, (
        f"Station {STATION_BERLIN} not found in station list"
    )


def test_dwd_station_list_count():
    from clarigrid.providers.dwd import dwd_station_list
    stations = dwd_station_list("air_temperature")
    # Germany has >400 active temperature stations.
    assert len(stations) >= 200, f"Expected ≥200 stations, got {len(stations)}"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_dwd_unknown_parameter_raises():
    with pytest.raises(ValueError, match="Unknown DWD parameter"):
        cg.get_weather(STATION_BERLIN, START, END,
                       source=PROVIDER, parameter="INVALID_PARAM", use_cache=False)


# ---------------------------------------------------------------------------
# Electricity method stubs
# ---------------------------------------------------------------------------


def test_dwd_get_prices_raises():
    from clarigrid.core.registry import get_provider
    p = get_provider(PROVIDER)
    with pytest.raises(NotImplementedError):
        p.get_prices(STATION_BERLIN, START, END)


def test_dwd_get_load_raises():
    from clarigrid.core.registry import get_provider
    p = get_provider(PROVIDER)
    with pytest.raises(NotImplementedError):
        p.get_load(STATION_BERLIN, START, END)


def test_dwd_get_generation_raises():
    from clarigrid.core.registry import get_provider
    p = get_provider(PROVIDER)
    with pytest.raises(NotImplementedError):
        p.get_generation(STATION_BERLIN, START, END)


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _cache._PARQUET_OK, reason="pyarrow not installed")
def test_dwd_use_cache_false_no_file_written():
    df = cg.get_weather(STATION_BERLIN, START, END,
                        source=PROVIDER, parameter="air_temperature", use_cache=False)
    assert not df.empty
    assert not _cache_file_exists(), "use_cache=False must not write cache"


@pytest.mark.skipif(not _cache._PARQUET_OK, reason="pyarrow not installed")
def test_dwd_cache_miss_then_hit():
    """First call writes cache; second returns cached data without network."""
    assert not _cache_file_exists()

    df1 = cg.get_weather(STATION_BERLIN, START, END,
                         source=PROVIDER, parameter="air_temperature", use_cache=True)
    assert not df1.empty
    assert _cache_file_exists()

    with patch.object(_http._SESSION, "get", side_effect=AssertionError("network hit on cache hit")):
        df2 = cg.get_weather(STATION_BERLIN, START, END,
                              source=PROVIDER, parameter="air_temperature", use_cache=True)

    assert not df2.empty
    pd.testing.assert_frame_equal(df1, df2)


@pytest.mark.skipif(not _cache._PARQUET_OK, reason="pyarrow not installed")
def test_dwd_cache_cleared_forces_network():
    df1 = cg.get_weather(STATION_BERLIN, START, END,
                         source=PROVIDER, parameter="air_temperature", use_cache=True)
    assert _cache_file_exists()

    removed = _cache.clear(PROVIDER)
    assert removed >= 1
    assert not _cache_file_exists()

    df2 = cg.get_weather(STATION_BERLIN, START, END,
                         source=PROVIDER, parameter="air_temperature", use_cache=True)
    assert not df2.empty
    pd.testing.assert_frame_equal(df1, df2)
