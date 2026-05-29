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


# ── Authentication errors ──────────────────────────────────────────────────

class ConfigurationError(AuthError):
    """No API key configured for a provider that requires one.

    Raised in non-interactive environments (CI, scripts) when the key is
    completely absent.  Message is fully self-contained — the Clarigrid
    website is not required to resolve it.
    """

    def __init__(self, source: str, cfg: "dict") -> None:
        self.source = source
        env_var: str = cfg["env_var"]
        reg_url: str = cfg["registration_url"]
        reg_instructions: str = cfg.get("registration_instructions", "")
        msg = (
            f"Clarigrid: no API key configured for '{source}'.\n"
            "\n"
            "To fix this, choose one of:\n"
            "\n"
            "  1. Interactive setup (opens browser):\n"
            f"       python -c \"import clarigrid; clarigrid.connect('{source}')\"\n"
            "\n"
            "  2. Manual setup (paste key directly):\n"
            f"       Add the following to ~/.config/clarigrid/.env:\n"
            f"         {env_var}=your-key-here\n"
            f"       Get your key at: {reg_url}\n"
            f"{reg_instructions}\n"
            "\n"
            "  3. Environment variable (CI / headless servers):\n"
            f"       export {env_var}=your-key-here"
        )
        super().__init__(msg)


class InvalidKeyError(AuthError):
    """API key found but rejected by the upstream service.

    Raised when the upstream API returns 401/403.  Never triggers a new
    auth flow — the user must explicitly re-run setup.
    """

    def __init__(self, source: str, env_var: str) -> None:
        self.source = source
        msg = (
            f"Clarigrid: the API key for '{source}' was rejected by the upstream service.\n"
            "\n"
            "This usually means the key has been revoked or has expired.\n"
            "\n"
            "To update your key:\n"
            f"  Interactive:  python -c \"import clarigrid; clarigrid.connect('{source}')\"\n"
            f"  Manual:       update {env_var} in ~/.config/clarigrid/.env"
        )
        super().__init__(msg)


class AuthTimeoutError(AuthError):
    """Browser authentication flow timed out (120 s default)."""

    def __init__(self, msg: str = "") -> None:
        if not msg:
            msg = (
                "Clarigrid: browser authentication timed out after 120 seconds.\n"
                "\n"
                "To complete setup manually:\n"
                "  clarigrid setup\n"
                "  or: python -c \"import clarigrid; clarigrid.connect('<source>')\""
            )
        super().__init__(msg)


class BrowserFlowError(AuthError):
    """Browser authentication flow failed (network error, bad callback, etc.)."""


class MissingDependencyError(ClarigridError):
    """Optional dependency required but not installed."""

    def __init__(self, package: str, feature: str) -> None:
        super().__init__(
            f"'{feature}' requires the '{package}' package.\n"
            f"Install it with: pip install {package}"
        )
