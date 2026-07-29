"""Tests for the Energinet Energi Data Service provider."""

from __future__ import annotations

import pandas as pd
import pytest

import clarigrid as cg
from clarigrid.providers import energinet as module
from clarigrid.providers.energinet import EnerginetProvider, _interval_energy_to_power
from tests.conftest import END, START, assert_valid_df


def test_energinet_registered():
    assert "energinet" in cg.list_providers()
    assert EnerginetProvider().zones() == {"DK1", "DK2"}


def test_energinet_quarter_hour_energy_converts_to_average_mw():
    index = pd.date_range("2025-01-01", periods=2, freq="15min", tz="UTC")
    frame = pd.DataFrame({"flow_sweden_mw": [5.0, 6.0]}, index=index)
    converted = _interval_energy_to_power(frame)
    assert converted.iloc[0, 0] == pytest.approx(20.0)
    assert converted.iloc[1, 0] == pytest.approx(24.0)


def test_energinet_prices_stitch_hourly_and_quarter_hour_schemas(monkeypatch):
    def fake_fetch(dataset, *args, **kwargs):
        if dataset == "Elspotprices":
            return [{"HourUTC": "2025-09-30T23:00:00", "SpotPriceEUR": 40.0}]
        return [{"TimeUTC": "2025-10-01T00:00:00", "DayAheadPriceEUR": 41.0}]

    monkeypatch.setattr(module, "_fetch_records", fake_fetch)
    frame = EnerginetProvider().get_prices(
        "DK1", "2025-09-30", "2025-10-02"
    )
    assert list(frame["price_eur_mwh"]) == [40.0, 41.0]
    assert str(frame.index.tz) == "UTC"
    assert frame.attrs["currency"] == "EUR"


def test_energinet_signed_border_energy_conversion(monkeypatch):
    records = [
        {
            "HourUTC": "2025-01-01T00:00:00",
            "ExchangeImportSE_MWh": 5.0,
            "ExchangeExportSE_MWh": -1.0,
        },
        {
            "HourUTC": "2025-01-01T00:15:00",
            "ExchangeImportSE_MWh": 3.0,
            "ExchangeExportSE_MWh": -2.0,
        },
    ]
    monkeypatch.setattr(module, "_fetch_records", lambda *args, **kwargs: records)
    frame = EnerginetProvider().get_physical_flows(
        "DK1", "2025-01-01", "2025-01-02"
    )
    assert frame.iloc[0]["flow_sweden_mw"] == pytest.approx(16.0)
    assert frame.iloc[1]["flow_sweden_mw"] == pytest.approx(4.0)
    assert frame.attrs["sign_convention"] == "positive=import, negative=export"


@pytest.mark.live
def test_energinet_legacy_prices():
    frame = cg.get_prices("DK1", START, END, source="energinet", use_cache=False)
    assert_valid_df(frame, expected_cols=["price_mwh"])
    assert frame.attrs.get("currency") == "EUR"


@pytest.mark.live
def test_energinet_load_and_generation_reuse_source_table():
    load = cg.get_load("DK1", START, END, source="energinet", use_cache=False)
    generation = cg.get_generation("DK1", START, END, source="energinet", use_cache=False)
    assert_valid_df(load, expected_cols=["load_mw"])
    assert_valid_df(generation, expected_cols=["wind_onshore_mw", "solar_mw"])


@pytest.mark.live
def test_energinet_generation_forecast():
    frame = cg.get_generation_forecast(
        "DK1", START, END, source="energinet", use_cache=False
    )
    assert_valid_df(
        frame,
        expected_cols=["wind_onshore_forecast_mw", "wind_offshore_forecast_mw"],
    )


@pytest.mark.live
def test_energinet_physical_flows():
    frame = cg.get_physical_flows(
        "DK1", START, END, source="energinet", use_cache=False
    )
    assert_valid_df(frame, expected_cols=["flow_sweden_mw", "flow_germany_mw"])


@pytest.mark.live
def test_energinet_co2_actual_and_forecast():
    actual = cg.get_co2_intensity(
        "DK1", START, END, source="energinet", use_cache=False
    )
    forecast = cg.get_co2_forecast(
        "DK1", START, END, source="energinet", use_cache=False
    )
    assert_valid_df(actual, expected_cols=["co2_consumption_g_kwh"])
    assert_valid_df(forecast, expected_cols=["co2_forecast_g_kwh"])
