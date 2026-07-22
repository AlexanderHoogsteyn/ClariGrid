"""Tests for the New York ISO public CSV provider."""

from __future__ import annotations

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

import clarigrid as cg
import clarigrid.providers.nyiso as module
from clarigrid.core.router import ZoneRouter
from clarigrid.providers.nyiso import NYISOProvider
from tests.conftest import assert_valid_df

START = "2025-01-06T05:00Z"
END = "2025-01-07T05:00Z"


def _zip_csv(name: str, csv: str) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr(name, csv)
    return buffer.getvalue()


_PRICE_CSV = "\n".join([
    "Time Stamp,Name,PTID,LBMP ($/MWHr),"
    "Marginal Cost Losses ($/MWHr),Marginal Cost Congestion ($/MWHr)",
    "01/06/2025 00:00,N.Y.C.,61761,45.50,1.0,2.0",
    "01/06/2025 01:00,N.Y.C.,61761,46.25,1.0,2.0",
    "01/06/2025 00:00,WEST,61752,35.00,1.0,2.0",
])

_LOAD_CSV = "\n".join([
    '"Time Stamp","Time Zone","Name","PTID","Integrated Load"',
    '"01/06/2025 00:00:00","EST","N.Y.C.",61761,6000.0',
    '"01/06/2025 00:00:00","EST","WEST",61752,2000.0',
    '"01/06/2025 01:00:00","EST","N.Y.C.",61761,6100.0',
    '"01/06/2025 01:00:00","EST","WEST",61752,2100.0',
])

_FUEL_CSV = "\n".join([
    "Time Stamp,Time Zone,Fuel Category,Gen MW",
    "01/06/2025 00:05:00,EST,Natural Gas,3000",
    "01/06/2025 00:05:00,EST,Nuclear,3200",
    "01/06/2025 00:05:00,EST,Wind,500",
])

_FORECAST_CSV = "\n".join([
    '"Time Stamp","Capitl","Centrl","Dunwod","Genese","Hud Vl","Longil",'
    '"Mhk Vl","Millwd","N.Y.C.","North","West","NYISO"',
    '"01/06/2025 00:00",100,200,300,400,500,600,700,800,900,1000,1100,6600',
    '"01/06/2025 01:00",110,210,310,410,510,610,710,810,910,1010,1110,6710',
])


def _fake_download(url: str, timeout: int) -> bytes:
    if "/damlbmp/" in url:
        return _zip_csv("20250106damlbmp_zone.csv", _PRICE_CSV)
    if "/palIntegrated/" in url:
        return _zip_csv("20250106palIntegrated.csv", _LOAD_CSV)
    if "/rtfuelmix/" in url:
        return _zip_csv("20250106rtfuelmix.csv", _FUEL_CSV)
    if "/isolf/" in url:
        return _zip_csv("20250106isolf.csv", _FORECAST_CSV)
    raise AssertionError(f"Unexpected NYISO URL: {url}")


def test_nyiso_registered_with_per_capability_coverage():
    assert "nyiso" in cg.list_providers()
    coverage = NYISOProvider().capability_zones()
    assert "NYISO_NYC" in coverage["prices"]
    assert "NYIS" not in coverage["prices"]
    assert "NYIS" in coverage["generation"]
    assert "NYISO_NYC" not in coverage["generation"]

    router = ZoneRouter()
    router.register_coverage("nyiso", coverage)
    assert router.resolve("NYISO_NYC", "prices") == "nyiso"
    assert router.resolve("NYIS", "generation") == "nyiso"


def test_nyiso_price_and_currency_contract(monkeypatch):
    monkeypatch.setattr(module, "get_bytes", _fake_download)
    frame = cg.get_prices(
        "NYISO_NYC", START, END, source="nyiso", use_cache=False
    )
    assert_valid_df(frame, expected_cols=["price_mwh"], min_rows=2)
    assert frame.iloc[0]["price_mwh"] == pytest.approx(45.5)
    assert frame.attrs["currency"] == "USD"
    assert frame.attrs["location"] == "N.Y.C."


def test_nyiso_system_load_sums_zones_and_zonal_load_selects(monkeypatch):
    monkeypatch.setattr(module, "get_bytes", _fake_download)
    system = cg.get_load("NYIS", START, END, source="nyiso", use_cache=False)
    nyc = cg.get_load("NYISO_NYC", START, END, source="nyiso", use_cache=False)

    assert_valid_df(system, expected_cols=["load_mw"], min_rows=2)
    assert system.iloc[0]["load_mw"] == pytest.approx(8000.0)
    assert nyc.iloc[0]["load_mw"] == pytest.approx(6000.0)


def test_nyiso_generation_and_forecast_contracts(monkeypatch):
    monkeypatch.setattr(module, "get_bytes", _fake_download)
    generation = cg.get_generation(
        "NYIS", START, END, source="nyiso", use_cache=False
    )
    forecast = cg.get_load_forecast(
        "NYIS", START, END, source="nyiso", use_cache=False
    )

    assert_valid_df(
        generation, expected_cols=["gas_mw", "nuclear_mw", "wind_mw"]
    )
    assert generation.iloc[0]["gas_mw"] == pytest.approx(3000)
    assert_valid_df(forecast, expected_cols=["load_forecast_mw"], min_rows=2)
    assert forecast.iloc[0]["load_forecast_mw"] == pytest.approx(6600)


@pytest.mark.live
def test_nyiso_prices_load_generation_and_forecast_live():
    prices = cg.get_prices(
        "NYISO_NYC", START, END, source="nyiso", use_cache=False
    )
    load = cg.get_load("NYIS", START, END, source="nyiso", use_cache=False)
    generation = cg.get_generation(
        "NYIS", START, END, source="nyiso", use_cache=False
    )
    forecast = cg.get_load_forecast(
        "NYIS", START, END, source="nyiso", use_cache=False
    )

    assert_valid_df(prices, expected_cols=["price_mwh"], min_rows=24)
    assert prices.attrs["currency"] == "USD"
    assert_valid_df(load, expected_cols=["load_mw"], min_rows=24)
    assert_valid_df(
        generation, expected_cols=["gas_mw", "nuclear_mw", "wind_mw"]
    )
    assert_valid_df(forecast, expected_cols=["load_forecast_mw"], min_rows=24)
