"""Tests for the Red Electrica REData provider."""

from __future__ import annotations

import pandas as pd
import pytest

import clarigrid as cg
from clarigrid.core.router import ZoneRouter
from clarigrid.providers.redata import (
    REDataProvider,
    _flow_frame,
    _indicator_frame,
    _period_hours,
)
from tests.conftest import assert_valid_df


def _indicator(title: str, values: list[tuple[str, float]], **attributes):
    return {
        "type": title,
        "attributes": {
            "title": title,
            "composite": False,
            "values": [
                {"datetime": timestamp, "value": value}
                for timestamp, value in values
            ],
            **attributes,
        },
    }


def test_redata_registered_with_explicit_es_coverage():
    assert "redata" in cg.list_providers()
    provider = REDataProvider()
    assert provider.zones() == {"ES"}
    assert "prices" not in provider.capabilities()

    router = ZoneRouter()
    router.register_coverage("redata", provider.capability_zones())
    assert router.resolve("ES", "generation") == "redata"
    assert router.resolve("ES", "prices") is None


def test_redata_daily_energy_conversion_uses_dst_day_length():
    assert _period_hours("2025-03-30T00:00:00+01:00", "day") == 23
    assert _period_hours("2025-10-26T00:00:00+02:00", "day") == 25


def test_redata_nested_generation_is_flattened_and_converted_to_mw():
    payload = {
        "included": [{
            "attributes": {
                "title": "Renovable",
                "content": [
                    _indicator(
                        "Eolica",
                        [("2025-03-30T00:00:00+01:00", 2300.0)],
                    ),
                    _indicator(
                        "Generacion renovable",
                        [("2025-03-30T00:00:00+01:00", 9999.0)],
                        composite=True,
                    ),
                ],
            },
        }],
    }
    frame = _indicator_frame(
        payload, {"eolica": "wind_onshore_mw"}, energy_period="day"
    )
    assert frame.iloc[0]["wind_onshore_mw"] == pytest.approx(100.0)
    assert list(frame.columns) == ["wind_onshore_mw"]
    assert str(frame.index.tz) == "UTC"


def test_redata_realtime_demand_maps_to_canonical_columns():
    payload = {
        "included": [
            _indicator("Real", [("2025-01-06T00:00:00+01:00", 25000)]),
            _indicator("Prevista", [("2025-01-06T00:00:00+01:00", 25200)]),
        ],
    }
    frame = _indicator_frame(payload, {
        "real": "load_mw",
        "prevista": "load_forecast_mw",
    })
    assert frame.iloc[0]["load_mw"] == 25000
    assert frame.iloc[0]["load_forecast_mw"] == 25200
    assert frame.index[0] == pd.Timestamp("2025-01-05T23:00:00Z")


def test_redata_flow_balance_is_import_positive_average_mw():
    payload = {
        "included": [{
            "attributes": {
                "title": "Francia",
                "content": [
                    _indicator(
                        "saldo", [("2025-01-06T00:00:00+01:00", 2400.0)]
                    ),
                ],
            },
        }],
    }
    frame = _flow_frame(payload)
    assert frame.iloc[0]["flow_fr_mw"] == pytest.approx(100.0)


def test_redata_generation_share_sums_to_100(monkeypatch):
    index = pd.DatetimeIndex(["2025-01-06T00:00:00Z"], name="utc_time")
    generation = pd.DataFrame(
        {"wind_onshore_mw": [75.0], "gas_mw": [25.0]}, index=index
    )
    monkeypatch.setattr(REDataProvider, "get_generation", lambda *args, **kwargs: generation)
    frame = REDataProvider().get_generation_share("ES", "2025-01-06", "2025-01-06")
    assert frame.iloc[0]["wind_onshore_share_pct"] == 75.0
    assert frame.sum(axis=1).iloc[0] == pytest.approx(100.0)


@pytest.mark.live
def test_redata_load_and_forecast_live():
    start, end = "2025-01-06T00:00", "2025-01-06T01:00"
    load = cg.get_load("ES", start, end, source="redata", use_cache=False)
    forecast = cg.get_load_forecast("ES", start, end, source="redata", use_cache=False)
    assert_valid_df(load, expected_cols=["load_mw"], min_rows=10)
    assert_valid_df(forecast, expected_cols=["load_forecast_mw"], min_rows=10)


@pytest.mark.live
def test_redata_generation_and_shares_live():
    generation = cg.get_generation(
        "ES", "2025-01-06", "2025-01-08", source="redata", use_cache=False
    )
    shares = cg.get_generation_share(
        "ES", "2025-01-06", "2025-01-08", source="redata", use_cache=False
    )
    assert_valid_df(
        generation, expected_cols=["wind_onshore_mw", "solar_mw", "nuclear_mw"]
    )
    assert_valid_df(shares, expected_cols=["wind_onshore_share_pct"])
    assert shares.sum(axis=1).dropna().between(99.9, 100.1).all()


@pytest.mark.live
def test_redata_flows_and_capacity_live():
    flows = cg.get_physical_flows(
        "ES", "2025-01-06", "2025-01-08", source="redata", use_cache=False
    )
    capacity = cg.get_installed_capacity(
        "ES", "2025-01-01", "2025-03-31", source="redata",
        time_step="monthly", use_cache=False,
    )
    assert_valid_df(flows, expected_cols=["flow_fr_mw", "flow_pt_mw"])
    assert flows.attrs.get("sign_convention") == "positive=import, negative=export"
    assert_valid_df(
        capacity, expected_cols=["wind_onshore_capacity_mw", "solar_capacity_mw"]
    )
