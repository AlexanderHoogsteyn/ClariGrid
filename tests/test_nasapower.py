"""Tests for the NASA POWER weather provider."""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

import clarigrid as cg
import clarigrid.core.cache as _cache
from clarigrid.core import http as _http
from clarigrid.providers.nasapower import (
    _parse_response,
    _parse_zone,
    _resolve_variables,
)
from tests.conftest import assert_valid_weather_df, median_interval

PROVIDER = "nasapower"
ZONE_NEW_YORK = "40.7128,-74.0060"
START = "2025-01-06"
END = "2025-01-08"


@pytest.fixture
def daily_payload() -> dict:
    return {
        "geometry": {"coordinates": [-74.006, 40.713, 10.17]},
        "properties": {
            "parameter": {
                "T2M": {"20250106": -0.4, "20250107": -999.0},
                "PS": {"20250106": 100.91, "20250107": 100.98},
                "ALLSKY_SFC_SW_DWN": {"20250106": 2.4, "20250107": 4.8},
            }
        },
        "header": {"fill_value": -999.0, "time_standard": "UTC"},
    }


def test_nasapower_registered():
    assert PROVIDER in cg.list_providers()


def test_parse_zone_validates_coordinates():
    assert _parse_zone(ZONE_NEW_YORK) == (40.7128, -74.006)
    with pytest.raises(ValueError, match="Latitude"):
        _parse_zone("91,0")
    with pytest.raises(ValueError, match="lat,lon"):
        _parse_zone("New York")


def test_variable_aliases_and_limit():
    assert _resolve_variables(["temperature_c", "WS10M"], "daily") == ["T2M", "WS10M"]
    assert _resolve_variables("temperature_c", "daily") == ["T2M"]
    assert len(_resolve_variables([f"PARAMETER_{index}" for index in range(20)], "daily")) == 20
    with pytest.raises(ValueError, match="at most 20"):
        _resolve_variables([f"PARAMETER_{index}" for index in range(21)], "daily")
    with pytest.raises(ValueError, match="at most 15"):
        _resolve_variables([f"PARAMETER_{index}" for index in range(16)], "hourly")


def test_daily_parser_normalises_names_units_and_missing_values(daily_payload):
    df = _parse_response(
        daily_payload,
        ["T2M", "PS", "ALLSKY_SFC_SW_DWN"],
        "daily",
    )

    assert_valid_weather_df(
        df,
        expected_cols=["temperature_c", "pressure_hpa", "shortwave_radiation_w_m2"],
        min_rows=2,
    )
    assert pd.isna(df.loc["2025-01-07", "temperature_c"])
    assert df.loc["2025-01-06", "pressure_hpa"] == pytest.approx(1009.1)
    assert df.loc["2025-01-06", "shortwave_radiation_w_m2"] == pytest.approx(100.0)
    assert df.attrs["units"]["pressure_hpa"] == "hPa"
    assert df.attrs["source_parameters"]["temperature_c"] == "T2M"
    assert df.attrs["provider"] == PROVIDER
    assert df.attrs["license_url"].startswith("https://www.earthdata.nasa.gov/")


@pytest.mark.live
def test_nasapower_daily_live():
    df = cg.get_weather(
        ZONE_NEW_YORK,
        START,
        END,
        source=PROVIDER,
        use_cache=False,
    )
    assert_valid_weather_df(
        df,
        expected_cols=[
            "temperature_c",
            "precipitation_mm",
            "wind_speed_ms",
            "humidity_pct",
            "shortwave_radiation_w_m2",
            "pressure_hpa",
        ],
        min_rows=3,
    )
    assert median_interval(df) == pd.Timedelta("1D")
    assert df["pressure_hpa"].dropna().between(800, 1100).all()


@pytest.mark.live
def test_nasapower_hourly_live():
    df = cg.get_weather(
        ZONE_NEW_YORK,
        START,
        START,
        source=PROVIDER,
        temporal="hourly",
        use_cache=False,
    )
    assert_valid_weather_df(
        df,
        expected_cols=[
            "temperature_c",
            "dew_point_c",
            "wind_direction_deg",
            "shortwave_radiation_w_m2",
        ],
        min_rows=24,
    )
    assert median_interval(df) == pd.Timedelta("1h")


@pytest.mark.live
@pytest.mark.skipif(not _cache._PARQUET_OK, reason="pyarrow not installed")
def test_weather_cache_distinguishes_temporal_and_variables():
    _cache.clear(PROVIDER)
    try:
        daily = cg.get_weather(
            ZONE_NEW_YORK,
            START,
            START,
            source=PROVIDER,
            temporal="daily",
            variables=["T2M"],
        )
        hourly = cg.get_weather(
            ZONE_NEW_YORK,
            START,
            START,
            source=PROVIDER,
            temporal="hourly",
            variables=["T2M"],
        )
        assert len(daily) == 1
        assert len(hourly) == 24

        with patch.object(
            _http._SESSION,
            "get",
            side_effect=AssertionError("network hit on cache hit"),
        ):
            cached_daily = cg.get_weather(
                ZONE_NEW_YORK,
                START,
                START,
                source=PROVIDER,
                temporal="daily",
                variables=["T2M"],
            )
        pd.testing.assert_frame_equal(daily, cached_daily)
    finally:
        _cache.clear(PROVIDER)
