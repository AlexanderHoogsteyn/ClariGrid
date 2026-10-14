"""Tests for the Gas Networks Ireland daily demand provider."""

from __future__ import annotations

import pandas as pd
import pytest

import clarigrid as cg
import clarigrid.core.cache as cache
from clarigrid.core.cache import CacheManager
from clarigrid.core.exceptions import ProviderError
from clarigrid.core.router import ZoneRouter
from clarigrid.providers import gni as module
from clarigrid.providers.gni import GNIProvider, _parse_records
from tests.conftest import assert_valid_df, median_interval

PROVIDER = "gni"
ZONE = "IE"


def _records() -> list[dict]:
    return [
        {
            "Date": "2025-01-01",
            "Daily Metered ": 10.0,
            "Non Daily Metered (NDM)": 20.0,
            "Large Daily Metered (LDM) non Power Gen": 30.0,
            "Power Generation": 40.0,
            "Total ROI demand": 100.0,
        },
        {
            "Date": "2025-01-02",
            "Daily Metered ": 11.0,
            "Non Daily Metered (NDM)": 21.0,
            "Large Daily Metered (LDM) non Power Gen": 31.0,
            "Power Generation": 41.0,
            "Total ROI demand": 104.0,
        },
    ]


def test_gni_registered_with_ie_gas_demand_coverage():
    assert PROVIDER in cg.list_providers()
    provider = GNIProvider()
    assert provider.capabilities() == {"gas_demand"}
    assert provider.zones() == {ZONE}

    router = ZoneRouter()
    router.register_coverage(PROVIDER, provider.capability_zones())
    assert router.resolve(ZONE, "gas_demand") == PROVIDER
    assert router.resolve(ZONE, "load") is None


def test_parser_normalises_daily_gwh_and_filters_inclusive_dates():
    frame = _parse_records(_records(), "2025-01-02", "2025-01-02")

    assert_valid_df(frame, expected_cols=["gas_demand_gwh"], min_rows=1)
    assert frame.index[0] == pd.Timestamp("2025-01-02T00:00:00Z")
    assert frame.iloc[0]["power_generation_gas_demand_gwh"] == pytest.approx(41.0)
    assert frame.attrs["unit"] == "GWh"
    assert frame.attrs["update_frequency"] == "quarterly"
    assert frame.attrs["rate_limit"] is None
    assert frame.attrs["coverage_start"] == "2025-01-01"
    assert frame.attrs["coverage_end"] == "2025-01-02"


def test_parser_rejects_schema_and_value_changes():
    missing = _records()
    missing[0].pop("Total ROI demand")
    with pytest.raises(ProviderError, match="missing required fields"):
        _parse_records(missing, "2025-01-01", "2025-01-02")

    invalid = _records()
    invalid[0]["Power Generation"] = "unknown"
    with pytest.raises(ProviderError, match="invalid values"):
        _parse_records(invalid, "2025-01-01", "2025-01-02")


def test_provider_discovers_current_json_resource_and_adds_metadata(monkeypatch):
    calls: list[tuple[str, dict | None]] = []

    def fake_get_json(url, params=None):
        calls.append((url, params))
        if url == module._PACKAGE_API:
            return {
                "success": True,
                "result": {
                    "metadata_modified": "2026-08-17T10:19:08",
                    "resources": [{
                        "format": "JSON",
                        "state": "active",
                        "url": "https://www.gasnetworks.ie/data/demand.json",
                    }],
                },
            }
        return _records()

    monkeypatch.setattr(module, "get_json", fake_get_json)
    frame = GNIProvider().get_gas_demand(ZONE, "2025-01-01", "2025-01-02")

    assert len(calls) == 2
    assert calls[0] == (module._PACKAGE_API, {"id": module._PACKAGE_ID})
    assert calls[1][0] == "https://www.gasnetworks.ie/data/demand.json"
    assert frame.attrs["data_updated_at"] == "2026-08-17T10:19:08"


def test_public_api_no_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "_manager", CacheManager(tmp_path))
    calls = 0

    def fake_demand(self, zone, start, end, **kwargs):
        nonlocal calls
        calls += 1
        return _parse_records(_records(), start, end)

    monkeypatch.setattr(GNIProvider, "get_gas_demand", fake_demand)
    for _ in range(2):
        frame = cg.get_gas_demand(
            ZONE, "2025-01-01", "2025-01-02", source=PROVIDER, use_cache=False
        )

    assert calls == 2
    assert frame.attrs["provider"] == PROVIDER
    assert frame.attrs["dataset"] == "gas_demand"
    assert not list(tmp_path.iterdir())


@pytest.mark.live
def test_gni_daily_demand_live_smoke():
    frame = cg.get_gas_demand(
        ZONE, "2025-01-01", "2025-01-03", source=PROVIDER, use_cache=False
    )
    assert_valid_df(frame, expected_cols=["gas_demand_gwh"], min_rows=3)
    assert median_interval(frame) == pd.Timedelta("1D")
