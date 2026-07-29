"""Unit and live tests for NESO Carbon Intensity API integration."""

from __future__ import annotations

import pytest

import clarigrid as cg
from clarigrid.providers.neso import _carbon_intensity_frame, _generation_share_frame
from tests.conftest import assert_valid_df


def test_neso_carbon_intensity_parser():
    records = [{
        "from": "2025-01-01T00:00Z",
        "to": "2025-01-01T00:30Z",
        "intensity": {"actual": 120, "forecast": 130, "index": "moderate"},
    }]
    frame = _carbon_intensity_frame(records, "actual")
    assert frame.iloc[0]["actual"] == 120
    assert str(frame.index.tz) == "UTC"


def test_neso_generation_share_parser():
    records = [{
        "from": "2025-01-01T00:00Z",
        "to": "2025-01-01T00:30Z",
        "generationmix": [
            {"fuel": "wind", "perc": 60.0},
            {"fuel": "gas", "perc": 40.0},
        ],
    }]
    frame = _generation_share_frame(records)
    assert frame.iloc[0]["wind_share_pct"] == 60.0
    assert frame.iloc[0]["gas_share_pct"] == 40.0


@pytest.mark.live
def test_neso_carbon_actual_and_forecast():
    actual = cg.get_co2_intensity(
        "GB", "2025-01-06", "2025-01-07", source="neso", use_cache=False
    )
    forecast = cg.get_co2_forecast(
        "GB", "2025-01-06", "2025-01-07", source="neso", use_cache=False
    )
    assert_valid_df(actual, expected_cols=["co2_consumption_g_kwh"])
    assert_valid_df(forecast, expected_cols=["co2_forecast_g_kwh"])


@pytest.mark.live
def test_neso_generation_share():
    frame = cg.get_generation_share(
        "GB", "2025-01-06", "2025-01-07", source="neso", use_cache=False
    )
    assert_valid_df(frame, expected_cols=["wind_share_pct", "gas_share_pct"])
    # NESO publishes each fuel share rounded to one decimal place.
    assert frame.sum(axis=1).between(99.8, 100.2).all()
