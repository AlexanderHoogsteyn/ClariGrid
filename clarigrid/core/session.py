"""Runtime session state: connected providers, zone router, output timezone.

All module-level globals here are the single source of truth for the
running session.  ``api.py`` delegates to these functions; nothing else
should import state directly.
"""

from __future__ import annotations

import pandas as pd

from clarigrid.core.router import ZoneRouter

# ── Module-level state ─────────────────────────────────────────────────────

_router = ZoneRouter()
_connected: dict[str, object] = {}   # provider_name → DataProvider instance
_output_tz: str = "UTC"


# ── Connection management ──────────────────────────────────────────────────

def connect(name: str) -> None:
    """Connect to a named provider and update the routing table.

    Prints a one-line summary showing how many zone/capability slots were
    added and how many existing slots were overwritten.

    Raises:
        KeyError: Provider not registered.
    """
    from clarigrid.core.registry import _registry

    if name not in _registry:
        from clarigrid.core.registry import list_providers
        available = list_providers()
        raise KeyError(
            f"Provider '{name}' not registered. "
            f"Available: {available}. "
            "Install the matching package and import it, or use a built-in provider."
        )

    provider = _registry[name]
    new_count, overwritten = _router.register_coverage(
        name, provider.capability_zones()
    )
    _connected[name] = provider

    # ── Build human-readable output ────────────────────────────────────
    over_count = len(overwritten)
    old_providers = sorted(set(overwritten.values()))

    parts = [f"Connected to {provider.name()}"]
    if new_count > 0:
        parts.append(f"{new_count} new table(s)")
    if over_count > 0:
        parts.append(
            f"overwriting {over_count} table(s) from "
            + ", ".join(f"'{p}'" for p in old_providers)
        )

    if len(parts) == 1:
        # Reconnect with no changes.
        print(f"{parts[0]} (already connected, no changes).")
    else:
        print(f"{parts[0]} — {', '.join(parts[1:])}.")


# ── Provider resolution ────────────────────────────────────────────────────

def resolve(zone: str, capability: str) -> tuple[str, object]:
    """Return ``(provider_name, provider)`` for zone + capability.

    *zone* must already be alias-resolved.

    Raises:
        ValueError: No connected provider covers this zone/capability combo,
            with a suggestion of which provider to connect.
    """
    name = _router.resolve(zone, capability)
    if name is not None:
        return name, _connected[name]

    # Build helpful error.
    suggestions = _router.suggest_providers(zone, capability)
    from clarigrid.core.exceptions import ZoneNotCoveredError
    raise ZoneNotCoveredError(zone, capability, suggestions)


# ── Timezone management ────────────────────────────────────────────────────

def set_output_tz(tz: str) -> None:
    """Set the output timezone for all DataFrames returned by the public API.

    Data is stored internally as UTC and converted at the output boundary.

    Args:
        tz: IANA timezone string, e.g. ``"Europe/Brussels"``, ``"UTC"``,
            ``"US/Eastern"``.

    Raises:
        ValueError: Unrecognised timezone string.
    """
    global _output_tz
    try:
        pd.Timestamp("2020-01-01").tz_localize(tz)
    except Exception:
        raise ValueError(
            f"Unknown timezone: {tz!r}.  "
            "Use an IANA timezone name, e.g. 'Europe/Brussels'."
        )
    _output_tz = tz


def get_output_tz() -> str:
    """Return the current output timezone string."""
    return _output_tz


def reset() -> None:
    """Reset all session state to defaults.

    Disconnects all providers, clears the zone router, and resets the output
    timezone to UTC.  Useful in tests and scripts that need a clean slate::

        import clarigrid as cg
        cg.reset()

    After calling ``reset()``, providers must be reconnected with
    ``cg.connect()`` before data can be fetched.
    """
    global _router, _connected, _output_tz
    _router = ZoneRouter()
    _connected = {}
    _output_tz = "UTC"


def apply_output_tz(df: pd.DataFrame) -> pd.DataFrame:
    """Convert a UTC DatetimeIndex to the current output timezone.

    No-op when output timezone is ``"UTC"`` and the index is already UTC.
    Always returns a copy when a conversion is performed.
    """
    if df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return df

    tz = _output_tz

    if df.index.tz is None:
        df = df.copy()
        df.index = df.index.tz_localize("UTC")

    if tz != "UTC":
        df = df.copy()
        df.index = df.index.tz_convert(tz)

    return df
