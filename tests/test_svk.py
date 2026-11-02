"""Tests for the Svenska kraftnät physical-flow provider."""

from __future__ import annotations

import pandas as pd
import pytest

import clarigrid as cg
import clarigrid.core.cache as cache
from clarigrid.core.cache import CacheManager
from clarigrid.core.exceptions import ProviderError
from clarigrid.core.router import ZoneRouter
from clarigrid.providers import svk as module
from clarigrid.providers.svk import SVKProvider, _fetch_rows, _parse_rows
from tests.conftest import assert_valid_df, median_interval

PROVIDER = "svk"
ZONE = "SE3"


def _row(timestamp: str, source: str, target: str, power: float) -> dict:
    return {
        "start_time_utc": timestamp,
        "from_bidding_zone": source,
        "to_bidding_zone": target,
        "power": power,
        "power_unit": "MW",
    }


def _fields() -> list[dict[str, str]]:
    return [{"id": name, "type": kind} for name, kind in module._REQUIRED_FIELDS.items()]


def test_svk_registered_with_swedish_flow_coverage():
    assert PROVIDER in cg.list_providers()
    provider = SVKProvider()
    assert provider.capabilities() == {"physical_flows"}
    assert provider.zones() == {"SE1", "SE2", "SE3", "SE4"}

    router = ZoneRouter()
    router.register_coverage(PROVIDER, provider.capability_zones())
    assert router.resolve(ZONE, "physical_flows") == PROVIDER
    assert router.resolve(ZONE, "load") is None


def test_parser_normalises_direction_and_utc_intervals():
    frame = _parse_rows(
        [
            _row("2026-10-31T00:00:00", "NO1", ZONE, 72.42),
            _row("2026-10-31T00:00:00", ZONE, "FI", 1111.54),
            _row("2026-10-31T00:15:00", "NO1", ZONE, 70.0),
            _row("2026-10-31T00:15:00", ZONE, "FI", 1100.0),
        ],
        ZONE,
    )

    assert_valid_df(frame, expected_cols=["flow_no1_mw", "flow_fi_mw"], min_rows=2)
    assert frame.iloc[0]["flow_no1_mw"] == pytest.approx(72.42)
    assert frame.iloc[0]["flow_fi_mw"] == pytest.approx(-1111.54)
    assert median_interval(frame) == pd.Timedelta("15min")
    assert frame.attrs["sign_convention"] == "positive=import, negative=export"
    assert frame.attrs["rate_limit"] is None


def test_parser_rejects_schema_unit_and_duplicate_changes():
    missing = _row("2026-10-31T00:00:00", "NO1", ZONE, 1.0)
    missing.pop("power")
    with pytest.raises(ProviderError, match="missing required fields"):
        _parse_rows([missing], ZONE)

    wrong_unit = _row("2026-10-31T00:00:00", "NO1", ZONE, 1.0)
    wrong_unit["power_unit"] = "kW"
    with pytest.raises(ProviderError, match="unexpected power unit"):
        _parse_rows([wrong_unit], ZONE)

    duplicate = _row("2026-10-31T00:00:00", "NO1", ZONE, 1.0)
    with pytest.raises(ProviderError, match="duplicate"):
        _parse_rows([duplicate, duplicate], ZONE)


def test_fetch_paginates_and_validates_live_schema(monkeypatch):
    rows = [
        _row("2026-10-31T00:00:00", "NO1", ZONE, 1.0),
        _row("2026-10-31T00:15:00", "NO1", ZONE, 2.0),
    ]
    calls: list[dict] = []

    def fake_get_json(url, params):
        calls.append(dict(params))
        offset = params["offset"]
        page = rows[offset : offset + 1]
        return {
            "success": True,
            "result": {
                "resource_id": module._RESOURCE_ID,
                "fields": _fields(),
                "records": page,
                "total": len(rows),
            },
        }

    monkeypatch.setattr(module, "get_json", fake_get_json)
    monkeypatch.setattr(module, "_PAGE_SIZE", 1)
    assert _fetch_rows("2026-10-31T00:00", "2026-10-31T00:30") == rows
    assert [call["offset"] for call in calls] == [0, 1]
    assert all(call["resource_id"] == module._RESOURCE_ID for call in calls)


def test_public_api_no_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "_manager", CacheManager(tmp_path))
    calls = 0

    def fake_flows(self, zone, start, end, **kwargs):
        nonlocal calls
        calls += 1
        return _parse_rows(
            [
                _row("2026-10-31T00:00:00", "NO1", zone, 1.0),
            ],
            zone,
        )

    monkeypatch.setattr(SVKProvider, "get_physical_flows", fake_flows)
    for _ in range(2):
        frame = cg.get_physical_flows(
            ZONE,
            "2026-10-31T00:00",
            "2026-10-31T00:15",
            source=PROVIDER,
            use_cache=False,
        )

    assert calls == 2
    assert frame.attrs["provider"] == PROVIDER
    assert frame.attrs["dataset"] == "physical_flows"
    assert not list(tmp_path.iterdir())


@pytest.mark.live
def test_svk_physical_flows_live_smoke():
    frame = cg.get_physical_flows(
        ZONE,
        "2026-10-31T00:00",
        "2026-10-31T01:00",
        source=PROVIDER,
        use_cache=False,
    )
    assert_valid_df(frame, expected_cols=["flow_fi_mw", "flow_no1_mw"], min_rows=4)
    assert median_interval(frame) == pd.Timedelta("15min")
