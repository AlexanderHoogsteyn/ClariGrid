"""Tests for the Finnish Meteorological Institute provider."""

from __future__ import annotations

import pandas as pd
import pytest

import clarigrid as cg
import clarigrid.core.cache as cache
from clarigrid.core.cache import CacheManager
from clarigrid.core.exceptions import ProviderError
from clarigrid.core.normalise import EnergyFrame
from clarigrid.core.router import ZoneRouter
from clarigrid.providers import fmi as module
from clarigrid.providers.fmi import FMIProvider, _parse_response, _parse_units
from tests.conftest import assert_valid_weather_df, median_interval

PROVIDER = "fmi"
ZONE = "100971"


def _metadata(unit: str = "degC") -> str:
    return f"""<?xml version="1.0"?>
<CompositeObservableProperty xmlns="http://inspire.ec.europa.eu/schemas/omop/2.9"
 xmlns:gml="http://www.opengis.net/gml/3.2" gml:id="prop-1">
 <component><ObservableProperty gml:id="t2m"><uom uom="{unit}"/></ObservableProperty></component>
 <component><ObservableProperty gml:id="ws_10min"><uom uom="m/s"/></ObservableProperty></component>
 <component><ObservableProperty gml:id="rh"><uom uom="%"/></ObservableProperty></component>
 <component><ObservableProperty gml:id="r_1h"><uom uom="mm"/></ObservableProperty></component>
</CompositeObservableProperty>"""


def _response(
    *,
    timestamp: str = "2026-11-08T00:00:00Z",
    station: str = ZONE,
    duplicate: bool = False,
) -> str:
    def member(parameter: str, values: list[tuple[str, str]]) -> str:
        points = "".join(
            f"<wml2:point><wml2:MeasurementTVP><wml2:time>{time}</wml2:time>"
            f"<wml2:value>{value}</wml2:value></wml2:MeasurementTVP></wml2:point>"
            for time, value in values
        )
        return f"""<wfs:member><omso:PointTimeSeriesObservation>
 <om:observedProperty xlink:href="https://opendata.fmi.fi/meta?observableProperty=observation&amp;param={parameter}&amp;language=eng"/>
 <om:featureOfInterest><sams:SF_SpatialSamplingFeature><sam:sampledFeature>
  <target:LocationCollection><target:member><target:Location>
   <gml:identifier codeSpace="{module._FMISID_CODESPACE}">{station}</gml:identifier>
   <gml:name codeSpace="{module._LOCATION_NAME_CODESPACE}">Helsinki Kaisaniemi</gml:name>
  </target:Location></target:member></target:LocationCollection>
 </sam:sampledFeature><sams:shape><gml:Point>
  <gml:pos>60.17523 24.94459</gml:pos>
 </gml:Point></sams:shape>
 </sams:SF_SpatialSamplingFeature></om:featureOfInterest>
 <om:result><wml2:MeasurementTimeseries>{points}</wml2:MeasurementTimeseries></om:result>
</omso:PointTimeSeriesObservation></wfs:member>"""

    times = [
        (timestamp, "6.3"),
        (
            (pd.Timestamp(timestamp) + pd.Timedelta(hours=1))
            .isoformat()
            .replace("+00:00", "Z"),
            "5.8",
        ),
    ]
    if duplicate:
        times.append(times[0])
    precipitation = [
        (time, "-1" if index == 0 else "NaN")
        for index, (time, _) in enumerate(times[:2])
    ]
    members = "".join(
        [
            member("t2m", times),
            member("ws_10min", [(time, "3.2") for time, _ in times[:2]]),
            member("rh", [(time, "87") for time, _ in times[:2]]),
            member("r_1h", precipitation),
        ]
    )
    return f"""<?xml version="1.0"?>
<wfs:FeatureCollection timeStamp="2026-11-09T09:23:32Z"
 xmlns:wfs="http://www.opengis.net/wfs/2.0"
 xmlns:om="http://www.opengis.net/om/2.0"
 xmlns:omso="http://inspire.ec.europa.eu/schemas/omso/3.0"
 xmlns:gml="http://www.opengis.net/gml/3.2"
 xmlns:wml2="http://www.opengis.net/waterml/2.0"
 xmlns:sam="http://www.opengis.net/sampling/2.0"
 xmlns:sams="http://www.opengis.net/samplingSpatial/2.0"
 xmlns:target="http://xml.fmi.fi/namespace/om/atmosphericfeatures/1.1"
 xmlns:xlink="http://www.w3.org/1999/xlink">{members}</wfs:FeatureCollection>"""


def test_fmi_registered_and_routes_weather_stations():
    assert PROVIDER in cg.list_providers()
    provider = FMIProvider()
    assert provider.capabilities() == {"weather"}
    assert provider.zones() == {"*"}

    router = ZoneRouter()
    router.register_coverage(PROVIDER, provider.capability_zones())
    assert router.resolve(ZONE, "weather") == PROVIDER


def test_unit_metadata_validation_rejects_upstream_change():
    _parse_units(_metadata(), ["t2m", "ws_10min", "rh", "r_1h"])
    with pytest.raises(ProviderError, match="unexpected unit"):
        _parse_units(_metadata("degF"), ["t2m"])


def test_parser_normalises_waterml_and_no_rain_marker():
    frame = _parse_response(
        _response(), ["t2m", "ws_10min", "rh", "r_1h"], ZONE
    )

    assert_valid_weather_df(
        frame,
        expected_cols=["temperature_c", "wind_speed_ms", "humidity_pct", "precipitation_mm"],
        min_rows=2,
    )
    assert median_interval(frame) == pd.Timedelta("1h")
    assert frame.iloc[0]["precipitation_mm"] == 0
    assert pd.isna(frame.iloc[1]["precipitation_mm"])
    assert frame.attrs["station_name"] == "Helsinki Kaisaniemi"
    assert frame.attrs["license"] == "CC BY 4.0"
    assert frame.attrs["api_version"] == "OGC WFS 2.0.0"
    assert frame.attrs["query_limit"] is None
    assert frame.attrs["rate_limit_requests_per_day"] == 20_000
    assert frame.attrs["units"]["precipitation_mm"] == "mm/1h"


def test_parser_rejects_station_duplicates_and_changed_schema():
    with pytest.raises(ProviderError, match="unexpected station"):
        _parse_response(_response(station="101004"), ["t2m", "ws_10min", "rh", "r_1h"], ZONE)
    with pytest.raises(ProviderError, match="duplicate"):
        _parse_response(_response(duplicate=True), ["t2m", "ws_10min", "rh", "r_1h"], ZONE)
    with pytest.raises(ProviderError, match="WFS response schema"):
        _parse_response("<error/>", ["t2m"], ZONE)
    changed_link = _response().replace("https://opendata.fmi.fi/meta", "https://example.com/meta")
    with pytest.raises(ProviderError, match="unexpected weather parameter"):
        _parse_response(changed_link, ["t2m", "ws_10min", "rh", "r_1h"], ZONE)


def test_provider_validates_inputs_and_chunks_bounded_requests(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake_get_text(url, params):
        calls.append((url, dict(params)))
        if url == module._META_URL:
            return _metadata()
        return _response(timestamp=params["starttime"])

    monkeypatch.setattr(module, "get_text", fake_get_text)
    frame = FMIProvider().get_weather(
        ZONE,
        "2026-01-01T00:30:00Z",
        "2026-02-02T02:30:00Z",
        variables=["temperature_c", "ws_10min", "humidity_pct", "r_1h"],
    )

    assert len(calls) == 3
    assert calls[0][0] == module._META_URL
    assert calls[1][1]["starttime"] == "2026-01-01T00:00:00Z"
    assert calls[1][1]["endtime"] == "2026-01-31T23:00:00Z"
    assert calls[2][1]["starttime"] == "2026-02-01T00:00:00Z"
    assert calls[2][1]["timestep"] == 60
    assert frame.index.min() >= pd.Timestamp("2026-01-01T00:30:00Z")

    with pytest.raises(ValueError, match="numeric fmisid"):
        FMIProvider().get_weather("Helsinki", "2026-11-08", "2026-11-08")
    with pytest.raises(ValueError, match="Unsupported FMI variable"):
        FMIProvider().get_weather(ZONE, "2026-11-08", "2026-11-08", variables=["uv"])


@pytest.mark.skipif(not cache._PARQUET_OK, reason="pyarrow not installed")
def test_public_api_cache_timezone_and_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "_manager", CacheManager(tmp_path))
    calls = 0

    def fake_weather(self, zone, start, end, **kwargs):
        nonlocal calls
        calls += 1
        frame = EnergyFrame(
            {"temperature_c": [6.3]},
            index=pd.DatetimeIndex(["2026-11-08T00:00:00Z"], name="utc_time"),
        )
        frame.attrs["license"] = "CC BY 4.0"
        return frame

    monkeypatch.setattr(FMIProvider, "get_weather", fake_weather)
    cg.reset()
    cg.connect(PROVIDER)
    try:
        first = cg.get_weather(ZONE, "2026-11-08", "2026-11-08", variables=["t2m"])
        cg.set_timezone("Europe/Helsinki")
        second = cg.get_weather(ZONE, "2026-11-08", "2026-11-08", variables=["t2m"])
        assert calls == 1
        assert first.attrs["provider"] == PROVIDER
        assert first.attrs["dataset"] == "weather"
        assert first.attrs["zone"] == ZONE
        assert first.attrs["license"] == "CC BY 4.0"
        assert str(second.index.tz) == "Europe/Helsinki"
    finally:
        cg.reset()


def test_public_api_no_cache_and_connected_routing(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "_manager", CacheManager(tmp_path))
    calls = 0

    def fake_weather(self, zone, start, end, **kwargs):
        nonlocal calls
        calls += 1
        return EnergyFrame(
            {"temperature_c": [6.3]},
            index=pd.DatetimeIndex(["2026-11-08T00:00:00Z"], name="utc_time"),
        )

    monkeypatch.setattr(FMIProvider, "get_weather", fake_weather)
    cg.reset()
    cg.connect(PROVIDER)
    try:
        for _ in range(2):
            frame = cg.get_weather(ZONE, "2026-11-08", "2026-11-08", use_cache=False)
        assert calls == 2
        assert frame.attrs["provider"] == PROVIDER
        assert not list(tmp_path.iterdir())
    finally:
        cg.reset()


@pytest.mark.live
def test_fmi_live_smoke():
    today = pd.Timestamp.now(tz="UTC").normalize()
    frame = cg.get_weather(
        ZONE,
        today - pd.Timedelta(days=1),
        today,
        source=PROVIDER,
        variables=["t2m", "ws_10min", "rh"],
        use_cache=False,
    )
    assert_valid_weather_df(
        frame,
        expected_cols=["temperature_c", "wind_speed_ms", "humidity_pct"],
        min_rows=12,
    )
    assert median_interval(frame) == pd.Timedelta("1h")
