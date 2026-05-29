"""Public API entry points. No provider-specific logic lives here.

Routing logic:
- When ``source=`` is omitted, ``session.resolve(zone, capability)`` selects
  the best connected provider for that zone + dataset combination.
- When ``source=`` is given, the named provider is used directly (bypasses
  the router).

All returned DataFrames (``EnergyFrame``) have:
- ``DatetimeIndex`` named ``utc_time``, converted to the output timezone set
  via ``cg.set_timezone()``.
- Canonical column names (``price_mwh``, ``load_mw``, ``*_mw`` for generation).
- ``df.attrs["currency"]`` on price DataFrames (``"EUR"`` or ``"GBP"``).
- Provenance: ``df.attrs["provider"]``, ``df.attrs["zone"]``,
  ``df.attrs["dataset"]``, ``df.attrs["fetched_at"]``.

Capability registry
-------------------
``_CAPABILITIES`` maps capability name → ``_CapabilitySpec``.  Adding a new
capability (e.g. ``"curtailment"``) means touching **one place**: add a row
here, add the abstract method on the interface, implement in providers, and
optionally add a public wrapper function.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import pandas as pd

from clarigrid.core import cache as _cache
from clarigrid.core import config as _config
from clarigrid.core import normalise as _norm
from clarigrid.core import session as _session
from clarigrid.core.normalise import EnergyFrame
from clarigrid.core.registry import register_provider  # noqa: F401 — re-exported
from clarigrid.utils.validation import resolve_zone, validate_date_range


# ── Capability registry ────────────────────────────────────────────────────

@dataclass(frozen=True)
class _CapabilitySpec:
    """Maps a capability string to a provider method name and normaliser."""

    provider_method: str
    normalise: Callable[[pd.DataFrame], EnergyFrame] | None = None


_CAPABILITIES: dict[str, _CapabilitySpec] = {
    "prices":     _CapabilitySpec("get_prices",     _norm.normalise_prices),
    "load":       _CapabilitySpec("get_load",       _norm.normalise_load),
    "generation": _CapabilitySpec("get_generation", _norm.normalise_generation),
    "weather":    _CapabilitySpec("get_weather",    None),
    "gas_flows":  _CapabilitySpec("get_gas_flows",  None),
    "capacity":   _CapabilitySpec("get_capacity",   None),
}


# ── Internal helpers ───────────────────────────────────────────────────────

def _stamp(
    df: pd.DataFrame,
    provider_name: str,
    dataset: str,
    zone: str,
) -> EnergyFrame:
    """Wrap *df* in EnergyFrame and attach provenance metadata."""
    if not isinstance(df, EnergyFrame):
        ef = EnergyFrame(df)
        # Migrate any existing attrs so currency etc. survive.
        if df.attrs:
            ef._set_meta(**{k: v for k, v in df.attrs.items()
                            if isinstance(v, (str, int, float, bool))})
    else:
        ef = df
    ef._set_meta(
        provider=provider_name,
        dataset=dataset,
        zone=zone,
        fetched_at=pd.Timestamp.now(tz="UTC").isoformat(),
    )
    return ef


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


def _fetch_and_normalise(
    capability: str,
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    provider_name: str,
    provider: object,
    use_cache: bool,
    cache_dataset: str | None = None,
    **provider_kwargs,
) -> EnergyFrame:
    """Shared fetch/cache/normalise/stamp logic for all capabilities.

    Args:
        capability: Key in ``_CAPABILITIES`` (e.g. ``"prices"``).
        zone: Already alias-resolved zone string.
        start: Start date.
        end: End date.
        provider_name: Resolved provider name (for cache keying + metadata).
        provider: Provider instance.
        use_cache: Whether to read from / write to the local cache.
        cache_dataset: Override cache dataset key (defaults to ``capability``).
        **provider_kwargs: Extra kwargs forwarded to the provider method.
    """
    spec = _CAPABILITIES[capability]
    cd = cache_dataset or capability

    if use_cache:
        cached = _cache.load(provider_name, cd, zone, start, end)
        if cached is not None:
            if spec.normalise is not None:
                cached = spec.normalise(cached)
            return _session.apply_output_tz(_stamp(cached, provider_name, capability, zone))

    method = getattr(provider, spec.provider_method)
    df = method(zone=zone, start=start, end=end, **provider_kwargs)

    if spec.normalise is not None:
        df = spec.normalise(df)
    df = _stamp(df, provider_name, capability, zone)

    if use_cache:
        _cache.save(df, provider_name, cd, zone, start, end)

    return _session.apply_output_tz(df)


# ── Connection & timezone ──────────────────────────────────────────────────

def connect(provider: str) -> None:
    """Connect to a provider and register it in the zone router.

    Multiple ``connect()`` calls accumulate coverage.  If two providers
    cover the same zone/capability pair the later call takes precedence.

    A summary is printed on success::

        Connected to SMARD (Bundesnetzagentur) — 21 new table(s).
        Connected to Elia Open Data (Belgium) — 2 new table(s).

    Args:
        provider: Registered provider name (e.g. ``"smard"``, ``"elia"``,
            ``"entsoe"``).

    Raises:
        KeyError: Provider not registered.
        ConfigurationError: Key-required provider, non-interactive, no key found.
        InvalidKeyError: Key found but rejected by the upstream API.
    """
    from clarigrid._auth import ensure_authenticated
    ensure_authenticated(provider)
    _session.connect(provider)


def reset() -> None:
    """Reset all session state to defaults.

    Disconnects all providers, clears the zone router, and resets the output
    timezone to UTC.  Useful in tests and scripts that need a clean slate::

        import clarigrid as cg
        cg.reset()

    After calling ``reset()``, providers must be reconnected with
    ``cg.connect()`` before data can be fetched.
    """
    _session.reset()


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


# ── Public data functions ──────────────────────────────────────────────────

def get_prices(
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    source: str | None = None,
    use_cache: bool = True,
) -> EnergyFrame:
    """Fetch day-ahead electricity prices.

    Args:
        zone: Bidding zone code (``"BE"``, ``"DE"``, ``"FR"`` …).
        start: Start date (inclusive).
        end: End date (inclusive).
        source: Override provider; defaults to the router-selected provider
            for this zone.
        use_cache: Read from / write to local Parquet cache.

    Returns:
        ``EnergyFrame`` with ``DatetimeIndex`` (``utc_time``, output timezone)
        and column ``price_mwh``.  ``df.attrs["currency"]`` holds the ISO
        currency code (``"EUR"`` for SMARD; ``"GBP"`` for Elexon).
    """
    zone = resolve_zone(zone)
    validate_date_range(start, end)
    provider_name, provider = _resolve_provider(zone, "prices", source)
    return _fetch_and_normalise(
        "prices", zone, start, end, provider_name, provider, use_cache
    )


def get_load(
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    source: str | None = None,
    use_cache: bool = True,
) -> EnergyFrame:
    """Fetch actual total load.

    Returns:
        ``EnergyFrame`` with ``DatetimeIndex`` (``utc_time``, output timezone)
        and column ``load_mw``.
    """
    zone = resolve_zone(zone)
    validate_date_range(start, end)
    provider_name, provider = _resolve_provider(zone, "load", source)
    return _fetch_and_normalise(
        "load", zone, start, end, provider_name, provider, use_cache
    )


def get_generation(
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    source: str | None = None,
    use_cache: bool = True,
) -> EnergyFrame:
    """Fetch actual generation per source.

    Returns:
        ``EnergyFrame`` with ``DatetimeIndex`` (``utc_time``, output timezone).
        Columns are generation fuel types in MW, e.g. ``solar_mw``,
        ``wind_onshore_mw``, ``nuclear_mw``.
    """
    zone = resolve_zone(zone)
    validate_date_range(start, end)
    provider_name, provider = _resolve_provider(zone, "generation", source)
    return _fetch_and_normalise(
        "generation", zone, start, end, provider_name, provider, use_cache
    )


def get_gas_flows(
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    indicator: str = "Physical Flow",
    period_type: str = "day",
    source: str | None = None,
    use_cache: bool = True,
) -> EnergyFrame:
    """Fetch gas physical flows or nominations.

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
        ``EnergyFrame`` with ``DatetimeIndex`` and columns ``flow_kwh_d``,
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
    cache_dataset = f"gas_{indicator.replace(' ', '_')}_{period_type}"
    return _fetch_and_normalise(
        "gas_flows", zone, start, end, provider_name, provider, use_cache,
        cache_dataset=cache_dataset,
        indicator=indicator, period_type=period_type,
    )


def get_capacity(
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    period_type: str = "day",
    source: str | None = None,
    use_cache: bool = True,
) -> EnergyFrame:
    """Fetch firm technical gas transmission capacity.

    Args:
        zone: Operator key (``"BE-TSO-0001"``), country code (``"BE"``),
            or interconnection point key.
        start: Start date (inclusive).
        end: End date (inclusive).
        period_type: Granularity — ``"hour"``, ``"day"``, ``"month"``.
        source: Override provider name.
        use_cache: Use local cache.

    Returns:
        ``EnergyFrame`` with ``DatetimeIndex`` and columns ``capacity_kwh_d``,
        ``direction``, ``point_key``, ``operator_key``.
    """
    validate_date_range(start, end)
    from clarigrid.core.interface import GasDataProvider

    provider_name, provider = _resolve_provider(zone, "capacity", source)
    if not isinstance(provider, GasDataProvider):
        raise TypeError(
            f"Provider '{provider_name}' does not support capacity data. "
            "Call cg.connect('entsog') first."
        )
    cache_dataset = f"capacity_{period_type}"
    return _fetch_and_normalise(
        "capacity", zone, start, end, provider_name, provider, use_cache,
        cache_dataset=cache_dataset,
        period_type=period_type,
    )


def get_weather(
    zone: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    source: str | None = None,
    use_cache: bool = True,
    **kwargs,
) -> EnergyFrame:
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
        ``EnergyFrame`` with ``DatetimeIndex``.  Columns depend on requested
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
    return _fetch_and_normalise(
        "weather", zone, start, end, provider_name, provider, use_cache,
        **kwargs,
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
    """Store an API key for a provider in ``~/.config/clarigrid/.env``."""
    _config.set_api_key(provider, key)


def list_providers() -> list[str]:
    """List all registered provider names."""
    from clarigrid.core.registry import list_providers as _list
    return _list()
