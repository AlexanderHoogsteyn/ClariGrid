"""Tests for Singapore's SINGSTAT monthly generation provider."""

from __future__ import annotations

import pandas as pd
import pytest

import clarigrid as cg
import clarigrid.core.api as api
import clarigrid.providers.singstat as module
from clarigrid.core.exceptions import ProviderError
from clarigrid.core.router import ZoneRouter
from clarigrid.providers.singstat import SingstatProvider, _generation_frame
from tests.conftest import assert_valid_df


def _payload() -> dict:
    return {
        "code": 0,
        "data": {
            "rows": [
                {
                    "vault_id": "1",
                    "DataSeries": "Electricity Generation",
                    "2024Mar": "744.0",
                    "2024Feb": "696.0",
                }
            ]
        },
        "errorMsg": "",
    }


def test_singstat_registered_and_routable():
    provider = SingstatProvider()
    assert "singstat" in cg.list_providers()
    assert provider.capabilities() == {"generation"}
    assert provider.zones() == {"SG"}

    router = ZoneRouter()
    router.register_coverage("singstat", provider.capability_zones())
    assert router.resolve("SG", "generation") == "singstat"


def test_singstat_parser_converts_monthly_gwh_to_average_mw():
    frame = _generation_frame(_payload(), "2024-02-01", "2024-04-01")

    assert_valid_df(frame, expected_cols=["total_generation_mw"], min_rows=2)
    assert frame.index.tolist() == [
        pd.Timestamp("2024-01-31T16:00:00Z"),
        pd.Timestamp("2024-02-29T16:00:00Z"),
    ]
    assert frame["total_generation_mw"].tolist() == pytest.approx([1000.0, 1000.0])


def test_singstat_parser_rejects_missing_series():
    with pytest.raises(ProviderError, match="missing the Electricity Generation"):
        _generation_frame({"code": 0, "data": {"rows": []}}, "2024-01-01", "2025-01-01")


def test_singstat_public_api_metadata_timezone_and_no_cache(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(module, "get_json", lambda url: calls.append(url) or _payload())
    monkeypatch.setattr(api._cache, "save", lambda *args, **kwargs: pytest.fail("cache write"))
    cg.set_timezone("Asia/Singapore")
    try:
        frame = cg.get_generation(
            "SG", "2024-02-01", "2024-04-01", source="singstat", use_cache=False
        )
    finally:
        cg.set_timezone("UTC")

    assert calls == [module._API_URL]
    assert str(frame.index.tz) == "Asia/Singapore"
    assert frame.index[0] == pd.Timestamp("2024-02-01T00:00:00+08:00")
    assert frame.attrs["provider"] == "singstat"
    assert frame.attrs["dataset_id"] == module._DATASET_ID
    assert frame.attrs["license"] == "Singapore Open Data Licence 1.0"
    assert frame.attrs["unit"] == "MW"


@pytest.mark.live
def test_singstat_monthly_generation_live():
    frame = cg.get_generation("SG", "2026-04-01", "2026-06-01", source="singstat", use_cache=False)
    assert_valid_df(frame, expected_cols=["total_generation_mw"], min_rows=2)
