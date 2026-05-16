"""Public API entry points. No provider-specific logic lives here."""

from __future__ import annotations

import pandas as pd

from clarigrid.core import cache as _cache
from clarigrid.core import config as _config
from clarigrid.core.registry import get_provider, list_providers, register_provider, set_default  # noqa: F401
from clarigrid.utils.validation import resolve_zone, validate_date_range


def connect(provider: str) -> None:
    """Set the active provider by name.

    The named provider must have been registered. Built-in free providers
    (entsog, smard, elia, neso, elexon) are registered automatically when
    ``clarigrid`` is imported.
    """
    set_default(provider)


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
        zone: Bidding zone code (``"BE"``, ``"DE"``, ``"FR"``…).
        start: Start date (inclusive).
        end: End date (inclusive).
        source: Override provider; defaults to the connected provider.
        use_cache: Read from / write to local Parquet cache.

    Returns:
        DataFrame with UTC ``DatetimeIndex`` (``utc_time``).
        Standard column: ``price_eur_mwh``.
        Note: Elexon returns ``system_sell_price_gbp_mwh`` /
        ``system_buy_price_gbp_mwh`` in GBP instead.
    """
    zone = resolve_zone(zone)
    validate_date_range(start, end)
    provider = get_provider(source)
    provider_name = source or _active_name()

    if use_cache:
        cached = _cache.load(provider_name, "prices", zone, start, end)
        if cached is not None:
            return cached

    df = provider.get_prices(zone=zone, start=start, end=end)
    if use_cache:
        _cache.save(df, provider_name, "prices", zone, start, end)
    return df


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
        DataFrame with UTC ``DatetimeIndex`` and column ``load_mw``.
    """
    zone = resolve_zone(zone)
    validate_date_range(start, end)
    provider = get_provider(source)
    provider_name = source or _active_name()

    if use_cache:
        cached = _cache.load(provider_name, "load", zone, start, end)
        if cached is not None:
            return cached

    df = provider.get_load(zone=zone, start=start, end=end)
    if use_cache:
        _cache.save(df, provider_name, "load", zone, start, end)
    return df


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
        DataFrame with UTC ``DatetimeIndex``. Columns are generation types in MW.
    """
    zone = resolve_zone(zone)
    validate_date_range(start, end)
    provider = get_provider(source)
    provider_name = source or _active_name()

    if use_cache:
        cached = _cache.load(provider_name, "generation", zone, start, end)
        if cached is not None:
            return cached

    df = provider.get_generation(zone=zone, start=start, end=end)
    if use_cache:
        _cache.save(df, provider_name, "generation", zone, start, end)
    return df


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

    Requires a gas provider to be connected (e.g. ``"entsog"``).

    Args:
        zone: Operator key (``"BE-TSO-0001"``), country code (``"BE"``),
            or interconnection point key.
        start: Start date (inclusive).
        end: End date (inclusive).
        indicator: ENTSOG indicator code. Common values:
            ``"Physical Flow"``, ``"Nomination"``,
            ``"Firm Technical"``, ``"Allocated Capacity"``.
        period_type: Granularity — ``"hour"``, ``"day"``, ``"month"``.
        source: Override provider name.
        use_cache: Use local cache.

    Returns:
        DataFrame with UTC ``DatetimeIndex`` and columns:
        ``flow_kwh_d``, ``direction``, ``point_key``, ``operator_key``.
    """
    validate_date_range(start, end)
    from clarigrid.core.interface import GasDataProvider

    provider = get_provider(source)
    if not isinstance(provider, GasDataProvider):
        raise TypeError(
            f"Provider '{source or _active_name()}' is not a gas provider. "
            "Call cg.connect('entsog') first."
        )
    provider_name = source or _active_name()
    cache_key = f"gas_{indicator.replace(' ', '_')}_{period_type}"

    if use_cache:
        cached = _cache.load(provider_name, cache_key, zone, start, end)
        if cached is not None:
            return cached

    df = provider.get_gas_flows(
        zone=zone, start=start, end=end, indicator=indicator, period_type=period_type
    )
    if use_cache:
        _cache.save(df, provider_name, cache_key, zone, start, end)
    return df


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

    Requires a weather provider to be connected (e.g. ``"openmeteo"``,
    ``"rmi"``, ``"dwd"``).

    Args:
        zone: Location identifier — format is provider-specific.
            Open-Meteo: ``"lat,lon"`` (e.g. ``"50.85,4.35"``).
            RMI: SYNOP station ID (e.g. ``"6447"``).
            DWD: 5-digit station ID (e.g. ``"02564"``).
        start: Start date (inclusive).
        end: End date (inclusive).
        source: Override provider name.
        use_cache: Use local Parquet cache. Note: cache key does not encode
            ``**kwargs`` — set ``use_cache=False`` when varying variables.
        **kwargs: Passed through to the provider (e.g. ``variables``,
            ``parameter``, ``dataset``, ``endpoint``).

    Returns:
        DataFrame with UTC ``DatetimeIndex``. Columns depend on requested
        variables.
    """
    validate_date_range(start, end)
    from clarigrid.core.interface import WeatherDataProvider

    provider = get_provider(source)
    if not isinstance(provider, WeatherDataProvider):
        raise TypeError(
            f"Provider '{source or _active_name()}' does not support weather data. "
            "Connect a weather provider first, e.g. cg.connect('openmeteo')."
        )
    provider_name = source or _active_name()

    if use_cache:
        cached = _cache.load(provider_name, "weather", zone, start, end)
        if cached is not None:
            return cached

    df = provider.get_weather(zone=zone, start=start, end=end, **kwargs)
    if use_cache:
        _cache.save(df, provider_name, "weather", zone, start, end)
    return df


def configure(path: str | None = None) -> None:
    """Reload configuration, optionally from a custom file path."""
    _config.configure(path)


def set_api_key(provider: str, key: str) -> None:
    """Store an API key for a provider in local config."""
    _config.set_api_key(provider, key)


def list_providers() -> list[str]:
    """List all registered provider names."""
    from clarigrid.core.registry import list_providers as _list
    return _list()


def _active_name() -> str:
    from clarigrid.core.registry import _default_provider
    return _default_provider or "unknown"
