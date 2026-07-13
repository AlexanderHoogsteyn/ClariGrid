"""Clarigrid — unified European energy market data SDK."""

# Auto-register all built-in free providers FIRST (no API key required).
# Must import before defining conflicting names in this namespace.
import clarigrid.providers  # noqa: E402, F401

from clarigrid.core import cache  # exposed as cg.cache
from clarigrid.core.api import (
    configure,
    connect,
    get_balancing_prices,
    get_balancing_volumes,
    get_co2_intensity,
    get_co2_forecast,
    get_commercial_schedule,
    get_frequency,
    get_gas_flows,
    get_gas_storage,
    get_generation,
    get_generation_forecast,
    get_generation_share,
    get_imbalance_prices,
    get_installed_capacity,
    get_load,
    get_load_forecast,
    get_lng_inventory,
    get_net_position,
    get_ntc,
    get_physical_flows,
    get_prices,
    get_residual_load,
    get_renewable_share,
    get_system_imbalance,
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
    "get_generation_share",
    "get_gas_flows",
    "get_gas_storage",
    "get_lng_inventory",
    "get_weather",
    "get_generation_forecast",
    "get_load_forecast",
    "get_residual_load",
    "get_imbalance_prices",
    "get_system_imbalance",
    "get_balancing_volumes",
    "get_balancing_prices",
    "get_physical_flows",
    "get_commercial_schedule",
    "get_installed_capacity",
    "get_ntc",
    "get_net_position",
    "get_co2_intensity",
    "get_co2_forecast",
    "get_frequency",
    "get_renewable_share",
    "set_api_key",
    "set_timezone",
    "list_providers",
    "register_provider",
    "cache",
]

__version__ = "0.1.0"
