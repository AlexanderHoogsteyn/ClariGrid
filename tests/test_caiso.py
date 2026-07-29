"""Tests for the California ISO OASIS provider."""

from __future__ import annotations

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

import clarigrid as cg
import clarigrid.providers.caiso as module
from clarigrid.providers.caiso import CAISOProvider, _price_frame, _read_zip
from tests.conftest import assert_valid_df


def _zip_csv(csv: str) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("prices.csv", csv)
    return buffer.getvalue()


def _sample_csv() -> str:
    return "\n".join([
        "INTERVALSTARTTIME_GMT,NODE,MARKET_RUN_ID,XML_DATA_ITEM,MW",
        "2025-01-06T08:00:00-00:00,TH_NP15_GEN-APND,DAM,LMP_PRC,45.25",
        "2025-01-06T08:00:00-00:00,TH_NP15_GEN-APND,DAM,LMP_ENE_PRC,44.00",
        "2025-01-06T09:00:00-00:00,TH_NP15_GEN-APND,DAM,LMP_PRC,46.50",
    ])


def test_caiso_registered_with_hub_zones():
    assert "caiso" in cg.list_providers()
    provider = CAISOProvider()
    assert provider.capabilities() == {"prices"}
    assert provider.zones() == {"CISO_NP15", "CISO_SP15", "CISO_ZP26"}


def test_caiso_zip_and_lmp_component_parser():
    raw = _read_zip(_zip_csv(_sample_csv()))
    frame = _price_frame(raw, "TH_NP15_GEN-APND")

    assert_valid_df(frame, expected_cols=["price_usd_mwh"], min_rows=2)
    assert frame.iloc[0]["price_usd_mwh"] == pytest.approx(45.25)
    assert frame.iloc[1]["price_usd_mwh"] == pytest.approx(46.50)


def test_caiso_public_price_contract_and_query(monkeypatch):
    calls: list[dict] = []

    def fake_get(url, params, timeout):
        calls.append({"url": url, "params": dict(params), "timeout": timeout})
        return _zip_csv(_sample_csv())

    monkeypatch.setattr(module, "get_bytes", fake_get)
    frame = cg.get_prices(
        "CISO_NP15",
        "2025-01-06T08:00Z",
        "2025-01-06T10:00Z",
        source="caiso",
        use_cache=False,
    )

    assert_valid_df(frame, expected_cols=["price_mwh"], min_rows=2)
    assert frame.attrs["currency"] == "USD"
    assert frame.attrs["location"] == "TH_NP15_GEN-APND"
    assert calls[0]["params"]["queryname"] == "PRC_LMP"
    assert calls[0]["params"]["market_run_id"] == "DAM"
    assert calls[0]["params"]["node"] == "TH_NP15_GEN-APND"


def test_caiso_arbitrary_node_requires_explicit_source(monkeypatch):
    monkeypatch.setattr(module, "get_bytes", lambda *args, **kwargs: _zip_csv(_sample_csv()))
    frame = cg.get_prices(
        "CISO",
        "2025-01-06T08:00Z",
        "2025-01-06T10:00Z",
        source="caiso",
        node="TH_NP15_GEN-APND",
        use_cache=False,
    )
    assert frame.attrs["location"] == "TH_NP15_GEN-APND"


def test_caiso_rejects_unsupported_market():
    with pytest.raises(ValueError, match="day_ahead"):
        CAISOProvider().get_prices(
            "CISO_NP15", "2025-01-06", "2025-01-07", market="real_time"
        )


@pytest.mark.live
def test_caiso_np15_day_ahead_prices_live():
    frame = cg.get_prices(
        "CISO_NP15",
        "2025-01-06T08:00Z",
        "2025-01-07T08:00Z",
        source="caiso",
        use_cache=False,
    )
    assert_valid_df(frame, expected_cols=["price_mwh"], min_rows=24)
    assert frame.attrs["currency"] == "USD"
    assert frame.attrs["location"] == "TH_NP15_GEN-APND"
