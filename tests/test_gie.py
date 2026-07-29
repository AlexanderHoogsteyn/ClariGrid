"""Tests for the GIE AGSI/ALSI provider."""

from __future__ import annotations

import pytest

import clarigrid as cg
from clarigrid.core import config
from clarigrid.providers import gie as module
from clarigrid.providers.gie import GieProvider, _frame, _scope
from tests.conftest import assert_valid_df

HAS_KEY = bool(config.get_api_key("gie"))


def test_gie_registered_and_country_aliases_are_gas_compatible():
    assert "gie" in cg.list_providers()
    assert GieProvider().capabilities() == {"gas_storage", "lng_inventory"}
    assert _scope("DE_LU") == {"country": "DE"}
    assert _scope("DK1") == {"country": "DK"}
    assert _scope("EU") == {"type": "eu"}


def test_gie_storage_units_convert_to_mwh_and_mwh_per_day():
    frame = _frame([{
        "gasDayStart": "2025-01-01",
        "name": "Belgium",
        "code": "BE",
        "gasInStorage": "1.5",
        "workingGasVolume": "2.0",
        "injection": "3.0",
        "withdrawal": "4.0",
        "netWithdrawal": "1.0",
        "injectionCapacity": "5.0",
        "withdrawalCapacity": "6.0",
        "full": "75.0",
        "status": "C",
    }], kind="storage")

    assert str(frame.index.tz) == "UTC"
    assert frame.iloc[0]["gas_in_storage_mwh"] == pytest.approx(1_500_000.0)
    assert frame.iloc[0]["working_gas_volume_mwh"] == pytest.approx(2_000_000.0)
    assert frame.iloc[0]["injection_mwh_d"] == pytest.approx(3_000.0)
    assert frame.iloc[0]["withdrawal_capacity_mwh_d"] == pytest.approx(6_000.0)
    assert frame.iloc[0]["storage_full_pct"] == pytest.approx(75.0)
    assert frame.iloc[0]["data_status"] == "C"


def test_gie_lng_inventory_keeps_volume_and_converts_sendout():
    frame = _frame([{
        "gasDayStart": "2025-01-01",
        "inventory": "100.5",
        "dtmi": "250.0",
        "sendOut": "4.2",
        "dtrs": "8.0",
    }], kind="lng")

    assert frame.iloc[0]["lng_inventory_thousand_m3"] == pytest.approx(100.5)
    assert frame.iloc[0]["lng_capacity_thousand_m3"] == pytest.approx(250.0)
    assert frame.iloc[0]["sendout_mwh_d"] == pytest.approx(4_200.0)
    assert frame.iloc[0]["sendout_capacity_mwh_d"] == pytest.approx(8_000.0)


def test_gie_provider_passes_facility_scope_and_metadata(monkeypatch):
    seen = {}

    def fake_fetch(base, zone, start, end, key, **kwargs):
        seen.update({"base": base, "zone": zone, "key": key, **kwargs})
        return [{"gasDayStart": "2025-01-01", "gasInStorage": 1}]

    monkeypatch.setattr(module, "_fetch", fake_fetch)
    monkeypatch.setattr(module.config, "get_api_key", lambda provider: "test-key")
    frame = GieProvider().get_gas_storage(
        "BE", "2025-01-01", "2025-01-02", company="operator", facility="store"
    )

    assert seen["company"] == "operator"
    assert seen["facility"] == "store"
    assert frame.attrs["attribution"] == "GIE AGSI"


def test_gie_facility_requires_company():
    with pytest.raises(ValueError, match="also require company"):
        module._fetch(
            module._AGSI,
            "BE",
            "2025-01-01",
            "2025-01-02",
            "key",
            facility="store",
        )


@pytest.mark.live
@pytest.mark.skipif(not HAS_KEY, reason="GIE API key not configured")
def test_gie_storage_live():
    frame = cg.get_gas_storage(
        "BE", "2025-01-01", "2025-01-07", source="gie", use_cache=False
    )
    assert_valid_df(frame, expected_cols=["gas_in_storage_mwh", "storage_full_pct"])


@pytest.mark.live
@pytest.mark.skipif(not HAS_KEY, reason="GIE API key not configured")
def test_gie_lng_live():
    frame = cg.get_lng_inventory(
        "BE", "2025-01-01", "2025-01-07", source="gie", use_cache=False
    )
    assert_valid_df(frame, expected_cols=["lng_inventory_thousand_m3", "sendout_mwh_d"])
