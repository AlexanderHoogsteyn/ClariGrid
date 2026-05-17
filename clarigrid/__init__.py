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


# ── First-run setup check ──────────────────────────────────────────────────

def _check_first_run() -> None:
    """Offer setup wizard on first import in an interactive terminal.

    Contract:
    - Exits in <50 ms when config already exists (single Path.exists() call).
    - No-op in non-interactive environments (CI, scripts, notebooks).
    - No network calls, no heavy imports unless the user opts in.
    """
    import sys
    from pathlib import Path

    # Fast path — single filesystem check, no further I/O.
    config_path = Path.home() / ".config" / "clarigrid" / ".env"
    old_config = Path.home() / ".clarigrid"  # backward-compat: skip wizard if old config exists

    if config_path.exists() or old_config.exists():
        return

    # Only prompt in fully interactive terminals.
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return
    except Exception:
        return

    print("\nWelcome to ClarigGrid!")
    print("No configuration found.")
    try:
        answer = input("Set up your data source connections now? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return

    if answer in ("", "y", "yes"):
        from clarigrid._auth import run_setup_wizard
        run_setup_wizard()
    else:
        print("\nYou can set up later:")
        print("  Call:  import clarigrid; clarigrid.connect('entsoe')")
        print("  Or run: clarigrid setup\n")


_check_first_run()
