"""Clarigrid — unified European energy market data SDK."""

# Auto-register all built-in free providers FIRST (no API key required).
# Must import before defining conflicting names in this namespace.
import clarigrid.providers  # noqa: E402, F401

from clarigrid.core import cache  # exposed as cg.cache
from clarigrid.core.api import (
    configure,
    connect,
    get_gas_flows,
    get_generation,
    get_load,
    get_prices,
    get_weather,
    list_providers,
    set_api_key,
    set_timezone,
    status,
)
from clarigrid.core.registry import register_provider

__all__ = [
    "connect",
    "configure",
    "status",
    "get_prices",
    "get_load",
    "get_generation",
    "get_gas_flows",
    "get_weather",
    "set_api_key",
    "set_timezone",
    "list_providers",
    "register_provider",
    "cache",
]

__version__ = "0.1.0"
