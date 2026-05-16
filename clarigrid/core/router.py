"""Zone-capability router.

Maps (zone, capability) → provider_name so that ``get_prices("BE")`` can
find the right provider without an explicit ``connect()`` override.

Wildcard providers (``zones() == {"*"}``) match any zone and are used as
fallbacks when no specific zone entry exists.  Specific zone registrations
always take precedence over wildcards.
"""

from __future__ import annotations

_WILDCARD = "*"


class ZoneRouter:
    """Routing table: (zone, capability) → provider_name.

    Two tiers:
    - *Specific*: provider declared an explicit zone list.  Stored as
      ``(zone_upper, capability) → name``.
    - *Wildcard*: provider declared ``{"*"}``.  Stored as
      ``capability → name``.  Used when no specific entry matches.

    Later ``register()`` calls overwrite earlier ones for the same slot,
    which implements the "latest ``connect()`` wins" rule.
    """

    def __init__(self) -> None:
        self._specific: dict[tuple[str, str], str] = {}
        self._wildcard: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        provider_name: str,
        zones: set[str],
        capabilities: set[str],
    ) -> tuple[int, dict[str, str]]:
        """Add provider to routing table.

        Args:
            provider_name: Registry key for the provider.
            zones: ``provider.zones()`` result.
            capabilities: ``provider.capabilities()`` result.

        Returns:
            ``(new_count, overwritten)`` where *overwritten* maps
            ``"zone/cap"`` → old_provider_name for every slot that was
            replaced.
        """
        # Resolve zone aliases before storing so that the lookup side
        # (which also uses resolved zones) finds a match.
        from clarigrid.utils.validation import resolve_zone

        overwritten: dict[str, str] = {}
        new_count = 0

        if _WILDCARD in zones:
            for cap in capabilities:
                old = self._wildcard.get(cap)
                if old is not None and old != provider_name:
                    overwritten[f"*/{cap}"] = old
                    self._wildcard[cap] = provider_name
                elif old is None:
                    self._wildcard[cap] = provider_name
                    new_count += 1
                # old == provider_name → reconnect, no change
        else:
            for raw_zone in zones:
                z = resolve_zone(raw_zone).upper()
                for cap in capabilities:
                    key = (z, cap)
                    old = self._specific.get(key)
                    if old is not None and old != provider_name:
                        overwritten[f"{raw_zone}/{cap}"] = old
                        self._specific[key] = provider_name
                    elif old is None:
                        self._specific[key] = provider_name
                        new_count += 1
                    # else: same provider reconnecting — skip

        return new_count, overwritten

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def resolve(self, zone: str, capability: str) -> str | None:
        """Return provider_name for (zone, capability), or None.

        *zone* must already be alias-resolved (i.e. ``resolve_zone()``
        has been called by the caller).
        """
        specific = self._specific.get((zone.upper(), capability))
        if specific is not None:
            return specific
        return self._wildcard.get(capability)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def suggest_providers(self, zone: str, capability: str) -> list[str]:
        """Return registered provider names that *could* cover zone+cap.

        Searches the global registry (not just connected providers).
        """
        from clarigrid.core.registry import _registry
        from clarigrid.utils.validation import resolve_zone

        resolved = resolve_zone(zone).upper()
        suggestions: list[str] = []
        for name, p in _registry.items():
            pzones = p.zones()
            if capability not in p.capabilities():
                continue
            if _WILDCARD in pzones:
                suggestions.append(name)
            elif resolved in {resolve_zone(z).upper() for z in pzones}:
                suggestions.append(name)
        return sorted(suggestions)

    def all_registered(self) -> dict[tuple[str, str], str]:
        """Return all registered ``(zone, cap) → provider_name`` pairs."""
        out: dict[tuple[str, str], str] = {}
        for cap, pname in self._wildcard.items():
            out[("*", cap)] = pname
        out.update({k: v for k, v in self._specific.items()})
        return out
