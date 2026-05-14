"""Provider registry — the only coupling point between core and providers."""

from __future__ import annotations

from typing import Dict

from clarigrid.core.interface import DataProvider

_registry: Dict[str, DataProvider] = {}
_default_provider: str | None = None


def register_provider(name: str, provider: DataProvider, *, default: bool = False) -> None:
    """Register a provider under a given name.

    Call this from provider packages (or their __init__.py) to make
    themselves available to the core API without modifying core code.

    Args:
        name: Short identifier used in cg.connect() / source= parameter.
        provider: Instantiated DataProvider.
        default: If True, set as the active default provider.
    """
    global _default_provider
    if not isinstance(provider, DataProvider):
        raise TypeError(f"Provider must subclass DataProvider, got {type(provider)}")
    _registry[name] = provider
    if default or _default_provider is None:
        _default_provider = name


def get_provider(name: str | None = None) -> DataProvider:
    """Resolve a provider by name, falling back to the current default."""
    target = name or _default_provider
    if target is None:
        raise RuntimeError(
            "No provider connected. Call cg.connect('entsoe') or register a provider."
        )
    if target not in _registry:
        raise KeyError(
            f"Provider '{target}' not registered. "
            f"Available: {list(_registry.keys()) or 'none'}. "
            "Install the matching clarigrid-<provider> package and import it."
        )
    return _registry[target]


def set_default(name: str) -> None:
    """Change the active default provider."""
    global _default_provider
    if name not in _registry:
        raise KeyError(f"Provider '{name}' not registered.")
    _default_provider = name


def list_providers() -> list[str]:
    """Return names of all registered providers."""
    return list(_registry.keys())
