"""Public API entry points. No provider-specific logic lives here.

Routing logic:
- When ``source=`` is omitted, ``session.resolve(zone, capability)`` selects
  the best connected provider for that zone + dataset combination.
- When ``source=`` is given, the named provider is used directly (bypasses
  the router).

All returned DataFrames have:
- ``DatetimeIndex`` named ``utc_time``, converted to the output timezone set
  via ``cg.set_timezone()``.
- Canonical column names (``price_mwh``, ``load_mw``, ``*_mw`` for generation).
- ``df.attrs["currency"]`` on price DataFrames (``"EUR"`` or ``"GBP"``).
"""

from __future__ import annotations

import pandas as pd

from clarigrid.core import cache as _cache
from clarigrid.core import config as _config
from clarigrid.core import normalise as _norm
from clarigrid.core import session as _session
from clarigrid.core.registry import register_provider  # noqa: F401 — re-exported
from clarigrid.utils.validation import resolve_zone, validate_date_range


def _stamp(
    df: pd.DataFrame,
    provider_name: str,
    dataset: str,
    zone: str,
) -> pd.DataFrame:
    """Attach provenance metadata to df.attrs (non-destructive)."""
    df.attrs["provider"] = provider_name
    df.attrs["dataset"] = dataset
    df.attrs["zone"] = zone
    df.attrs["fetched_at"] = pd.Timestamp.now(tz="UTC").isoformat()
    return df


# ── Connection & timezone ──────────────────────────────────────────────────

def connect(provider: str) -> None:
    """Connect to a provider and register it in the zone router.

    Multiple ``connect()`` calls accumulate coverage.  If two providers
    cover the same zone/capability pair the later call takes precedence.

    A summary is printed on success::

        Connected to SMARD (Bundesnetzagentur) — 21 new table(s).
        Connected to Elia Open Data (Belgium) — 2 new table(s).

    Args:
        provider: Registered provider name (e.g. ``"smard"``, ``"elia"``).

    Raises:
        KeyError: Provider not registered.
    """
    _session.connect(provider)


def set_timezone(tz: str) -> None:
    """Set the output timezone for all DataFrames returned by the public API.

    Data is always stored and cached as UTC.  The conversion to the requested
    timezone is applied at the output boundary, so the index name remains
    ``utc_time`` regardless of the active timezone.

    Args:
        tz: IANA timezone string, e.g. ``"Europe/Brussels"``, ``"UTC"``,
            ``"US/Eastern"``.  Run ``import pytz; pytz.all_timezones`` for
            a full list.

    Example::

        cg.set_timezone("Europe/Brussels")
        df = cg.get_prices("BE", "2025-01-01", "2025-01-07")
        # df.index is now in Europe/Brussels time
    """
    _session.set_output_tz(tz)


# ── Data retrieval helpers ─────────────────────────────────────────────────

def _resolve_provider(
    zone: str,
    capability: str,
    source: str | None,
) -> tuple[str, object]:
    """Return ``(provider_name, provider)`` for a data request."""
    if source is not None:
        from clarigrid.core.registry import _registry, list_providers
        if source not in _registry:
            raise KeyError(
                f"Provider '{source}' not registered.  "
                f"Available: {list_providers()}"
            )
        return source, _registry[source]
    return _session.resolve(zone, capability)


def _fetch_capability(
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    capability: str,
    method_name: str,
    source: str | None,
    use_cache: bool,
    cache_key: str | None = None,
    **kwargs,
) -> pd.DataFrame:
    """Generic fetch/cache/stamp pipeline for simple (already-tidy) datasets.

    Used by the secondary data functions (forecasts, balancing, cross-border,
    CO2 …) whose providers return their final canonical columns directly, so
    no shared ``normalise_*`` step is required.
    """
    zone = resolve_zone(zone)
    validate_date_range(start, end)
    provider_name, provider = _resolve_provider(zone, capability, source)

    method = getattr(provider, method_name, None)
    if method is None or capability not in provider.capabilities():
        raise TypeError(
            f"Provider '{provider_name}' does not support '{capability}'."
        )

    ck = cache_key or capability
    if use_cache:
        cached = _cache.load(provider_name, ck, zone, start, end)
        if cached is not None:
            return _session.apply_output_tz(cached)

    df = method(zone=zone, start=start, end=end, **kwargs)
    _stamp(df, provider_name, capability, zone)

    if use_cache:
        _cache.save(df, provider_name, ck, zone, start, end)
    return _session.apply_output_tz(df)


# ── Public data functions ──────────────────────────────────────────────────

def get_prices(
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    source: str | None = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Fetch day-ahead electricity prices.

    Args:
        zone: Bidding zone code (``"BE"``, ``"DE"``, ``"FR"`` …).
        start: Start date (inclusive).
        end: End date (inclusive).
        source: Override provider; defaults to the router-selected provider
            for this zone.
        use_cache: Read from / write to local Parquet cache.

    Returns:
        DataFrame with ``DatetimeIndex`` (``utc_time``, output timezone) and
        column ``price_mwh``.  ``df.attrs["currency"]`` holds the ISO
        currency code (``"EUR"`` for SMARD; ``"GBP"`` for Elexon).
    """
    zone = resolve_zone(zone)
    validate_date_range(start, end)
    provider_name, provider = _resolve_provider(zone, "prices", source)

    if use_cache:
        cached = _cache.load(provider_name, "prices", zone, start, end)
        if cached is not None:
            return _session.apply_output_tz(cached)

    df = provider.get_prices(zone=zone, start=start, end=end)
    df = _norm.normalise_prices(df)
    _stamp(df, provider_name, "prices", zone)

    if use_cache:
        _cache.save(df, provider_name, "prices", zone, start, end)
    return _session.apply_output_tz(df)


def get_load(
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    source: str | None = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Fetch actual total load.

    Returns:
        DataFrame with ``DatetimeIndex`` (``utc_time``, output timezone) and
        column ``load_mw``.
    """
    zone = resolve_zone(zone)
    validate_date_range(start, end)
    provider_name, provider = _resolve_provider(zone, "load", source)

    if use_cache:
        cached = _cache.load(provider_name, "load", zone, start, end)
        if cached is not None:
            return _session.apply_output_tz(cached)

    df = provider.get_load(zone=zone, start=start, end=end)
    df = _norm.normalise_load(df)
    _stamp(df, provider_name, "load", zone)

    if use_cache:
        _cache.save(df, provider_name, "load", zone, start, end)
    return _session.apply_output_tz(df)


def get_generation(
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    source: str | None = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Fetch actual generation per source.

    Returns:
        DataFrame with ``DatetimeIndex`` (``utc_time``, output timezone).
        Columns are generation fuel types in MW, e.g. ``solar_mw``,
        ``wind_onshore_mw``, ``nuclear_mw``.
    """
    zone = resolve_zone(zone)
    validate_date_range(start, end)
    provider_name, provider = _resolve_provider(zone, "generation", source)

    if use_cache:
        cached = _cache.load(provider_name, "generation", zone, start, end)
        if cached is not None:
            return _session.apply_output_tz(cached)

    df = provider.get_generation(zone=zone, start=start, end=end)
    df = _norm.normalise_generation(df)
    _stamp(df, provider_name, "generation", zone)

    if use_cache:
        _cache.save(df, provider_name, "generation", zone, start, end)
    return _session.apply_output_tz(df)


def get_gas_flows(
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    indicator: str = "Physical Flow",
    period_type: str = "day",
    source: str | None = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Fetch gas physical flows or capacity data.

    Args:
        zone: Operator key (``"BE-TSO-0001"``), country code (``"BE"``),
            or interconnection point key.
        start: Start date (inclusive).
        end: End date (inclusive).
        indicator: ENTSOG indicator.  Common values: ``"Physical Flow"``,
            ``"Nomination"``, ``"Firm Technical"``, ``"Allocated Capacity"``.
        period_type: Granularity — ``"hour"``, ``"day"``, ``"month"``.
        source: Override provider name.
        use_cache: Use local cache.

    Returns:
        DataFrame with ``DatetimeIndex`` and columns ``flow_kwh_d``,
        ``direction``, ``point_key``, ``operator_key``.
    """
    validate_date_range(start, end)
    from clarigrid.core.interface import GasDataProvider

    provider_name, provider = _resolve_provider(zone, "gas_flows", source)
    if not isinstance(provider, GasDataProvider):
        raise TypeError(
            f"Provider '{provider_name}' does not support gas flows. "
            "Call cg.connect('entsog') first."
        )
    cache_key = f"gas_{indicator.replace(' ', '_')}_{period_type}"

    if use_cache:
        cached = _cache.load(provider_name, cache_key, zone, start, end)
        if cached is not None:
            return _session.apply_output_tz(cached)

    df = provider.get_gas_flows(
        zone=zone, start=start, end=end,
        indicator=indicator, period_type=period_type,
    )
    _stamp(df, provider_name, "gas_flows", zone)

    if use_cache:
        _cache.save(df, provider_name, cache_key, zone, start, end)
    return _session.apply_output_tz(df)


def get_weather(
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    source: str | None = None,
    use_cache: bool = True,
    **kwargs,
) -> pd.DataFrame:
    """Fetch weather observations or forecast data.

    Args:
        zone: Location identifier — format is provider-specific.
            Open-Meteo: ``"lat,lon"`` (e.g. ``"50.85,4.35"``).
        start: Start date (inclusive).
        end: End date (inclusive).
        source: Override provider name.
        use_cache: Use local Parquet cache.  Note: cache key does not encode
            ``**kwargs`` — set ``use_cache=False`` when varying variables.
        **kwargs: Passed through to the provider (e.g. ``variables``).

    Returns:
        DataFrame with ``DatetimeIndex``.  Columns depend on requested
        variables.
    """
    validate_date_range(start, end)
    from clarigrid.core.interface import WeatherDataProvider

    provider_name, provider = _resolve_provider(zone, "weather", source)
    if not isinstance(provider, WeatherDataProvider):
        raise TypeError(
            f"Provider '{provider_name}' does not support weather data. "
            "Connect a weather provider first, e.g. cg.connect('openmeteo')."
        )

    if use_cache:
        cached = _cache.load(provider_name, "weather", zone, start, end)
        if cached is not None:
            return _session.apply_output_tz(cached)

    df = provider.get_weather(zone=zone, start=start, end=end, **kwargs)

    if use_cache:
        _cache.save(df, provider_name, "weather", zone, start, end)
    return _session.apply_output_tz(df)


# ── Forecasts ───────────────────────────────────────────────────────────────

def get_generation_forecast(
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    source: str | None = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Fetch wind/solar generation forecast (MW).

    Elia: per-horizon wind/solar forecasts (``wind_*``, ``solar_*`` columns).
    SMARD: day-ahead onshore/offshore/solar/combined forecast columns.
    """
    return _fetch_capability(
        zone, start, end,
        capability="generation_forecast",
        method_name="get_generation_forecast",
        source=source, use_cache=use_cache,
    )


def get_load_forecast(
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    source: str | None = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Fetch measured + day-ahead + week-ahead load forecast (MW)."""
    return _fetch_capability(
        zone, start, end,
        capability="load_forecast",
        method_name="get_load_forecast",
        source=source, use_cache=use_cache,
    )


def get_residual_load(
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    source: str | None = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Fetch residual load and pumped-storage consumption (MW).  SMARD only."""
    return _fetch_capability(
        zone, start, end,
        capability="residual_load",
        method_name="get_residual_load",
        source=source, use_cache=use_cache,
    )


# ── Imbalance & balancing (Elia) ──────────────────────────────────────────────

def get_imbalance_prices(
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    source: str | None = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Fetch quarter-hour imbalance prices (EUR/MWh) and components."""
    return _fetch_capability(
        zone, start, end,
        capability="imbalance_prices",
        method_name="get_imbalance_prices",
        source=source, use_cache=use_cache,
    )


def get_system_imbalance(
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    source: str | None = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Fetch system imbalance (SI) and balancing component volumes (MW)."""
    return _fetch_capability(
        zone, start, end,
        capability="system_imbalance",
        method_name="get_system_imbalance",
        source=source, use_cache=use_cache,
    )


def get_balancing_volumes(
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    source: str | None = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Fetch activated balancing energy volumes per product (MW)."""
    return _fetch_capability(
        zone, start, end,
        capability="balancing_volumes",
        method_name="get_balancing_volumes",
        source=source, use_cache=use_cache,
    )


def get_balancing_prices(
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    source: str | None = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Fetch activated balancing energy prices per product (EUR/MWh)."""
    return _fetch_capability(
        zone, start, end,
        capability="balancing_prices",
        method_name="get_balancing_prices",
        source=source, use_cache=use_cache,
    )


# ── Cross-border & capacity (Elia) ────────────────────────────────────────────

def get_physical_flows(
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    source: str | None = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Fetch cross-border physical flows per border (MW)."""
    return _fetch_capability(
        zone, start, end,
        capability="physical_flows",
        method_name="get_physical_flows",
        source=source, use_cache=use_cache,
    )


def get_commercial_schedule(
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    source: str | None = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Fetch day-ahead commercial exchange schedule per border (MW)."""
    return _fetch_capability(
        zone, start, end,
        capability="commercial_schedule",
        method_name="get_commercial_schedule",
        source=source, use_cache=use_cache,
    )


def get_ntc(
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    source: str | None = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Fetch net transfer capacity per border (MW)."""
    return _fetch_capability(
        zone, start, end,
        capability="ntc",
        method_name="get_ntc",
        source=source, use_cache=use_cache,
    )


def get_net_position(
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    source: str | None = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Fetch day-ahead implicit net position (MW; exports +, imports −)."""
    return _fetch_capability(
        zone, start, end,
        capability="net_position",
        method_name="get_net_position",
        source=source, use_cache=use_cache,
    )


# ── Environmental ─────────────────────────────────────────────────────────────

def get_co2_intensity(
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    source: str | None = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Fetch production- and consumption-based CO2 intensity (gCO2eq/kWh)."""
    return _fetch_capability(
        zone, start, end,
        capability="co2_intensity",
        method_name="get_co2_intensity",
        source=source, use_cache=use_cache,
    )


# ── Session introspection ──────────────────────────────────────────────────

def status() -> None:
    """Print a summary of connected providers, zones, and capabilities.

    Example output::

        Output timezone : Europe/Brussels
        Providers       : 3 connected

          Name                                Zones                     Capabilities
          ----                                -----                     ------------
          SMARD (Bundesnetzagentur)           DE_LU, AT, LU, …         generation, load, prices
          Elia Open Data (Belgium)            BE                        generation, load
          ENTSOG Transparency Platform        all zones (*)             capacity, gas_flows
    """
    connected = _session._connected
    tz = _session.get_output_tz()

    print(f"Output timezone : {tz}")
    print(f"Providers       : {len(connected)} connected")

    if not connected:
        print("\n  (none — call cg.connect() first)")
        return

    print()
    header = f"  {'Name':<38} {'Zones':<28} Capabilities"
    print(header)
    print("  " + "-" * (len(header) - 2))

    for _pname, provider in connected.items():
        zones = provider.zones()
        if "*" in zones:
            zone_str = "all zones (*)"
        else:
            zone_list = sorted(zones)
            zone_str = ", ".join(zone_list[:4])
            if len(zone_list) > 4:
                zone_str += f" (+{len(zone_list) - 4} more)"
        caps = ", ".join(sorted(provider.capabilities()))
        print(f"  {provider.name():<38} {zone_str:<28} {caps}")


# ── Configuration helpers ──────────────────────────────────────────────────

def configure(path: str | None = None) -> None:
    """Reload configuration, optionally from a custom file path."""
    _config.configure(path)


def set_api_key(provider: str, key: str) -> None:
    """Store an API key for a provider in ``~/.clarigrid/keys.toml``."""
    _config.set_api_key(provider, key)


def list_providers() -> list[str]:
    """List all registered provider names."""
    from clarigrid.core.registry import list_providers as _list
    return _list()
