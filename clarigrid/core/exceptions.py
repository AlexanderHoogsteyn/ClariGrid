"""Custom exception hierarchy for Clarigrid.

Catching ``ClarigridError`` handles all SDK errors.  More specific
subclasses allow callers to distinguish error types.

Example::

    import clarigrid as cg
    from clarigrid.core.exceptions import ZoneNotCoveredError, RateLimitError

    try:
        df = cg.get_prices("FR", "2025-01-01", "2025-01-07")
    except ZoneNotCoveredError as e:
        print(f"No provider for this zone: {e}")
    except RateLimitError:
        print("Hit API rate limit — try again later.")
"""

from __future__ import annotations


class ClarigridError(Exception):
    """Base class for all Clarigrid SDK errors."""


# ── Provider errors ────────────────────────────────────────────────────────

class ProviderError(ClarigridError):
    """Unexpected response or internal error from a data provider."""


class RateLimitError(ProviderError):
    """Provider returned HTTP 429 and retries were exhausted."""


class ProviderUnavailableError(ProviderError):
    """Provider returned repeated 5xx errors."""


class AuthError(ClarigridError):
    """Missing or invalid API key for a provider that requires authentication."""


# ── Routing / zone errors ──────────────────────────────────────────────────

class ZoneNotCoveredError(ClarigridError):
    """No connected provider supports the requested zone + capability.

    ``e.zone`` and ``e.capability`` hold the requested values.
    ``e.suggestions`` is a list of provider names that *could* cover this
    combination if connected.
    """

    def __init__(
        self,
        zone: str,
        capability: str,
        suggestions: list[str] | None = None,
    ) -> None:
        self.zone = zone
        self.capability = capability
        self.suggestions = suggestions or []
        msg = (
            f"No connected provider has '{capability}' data for zone '{zone}'."
        )
        if self.suggestions:
            msg += f"  Consider: cg.connect('{self.suggestions[0]}')"
            if len(self.suggestions) > 1:
                others = ", ".join(f"'{s}'" for s in self.suggestions[1:])
                msg += f" (or {others})"
        msg += "."
        super().__init__(msg)


# ── Cache errors ───────────────────────────────────────────────────────────

class CacheError(ClarigridError):
    """Cache read/write failure.  Treated as a cache miss by the API layer."""
