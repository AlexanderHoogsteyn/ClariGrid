"""Authentication layer for key-required data providers.

KeyState machine
----------------
  MISSING  → 1. try CLARIGRID_API_KEY to fetch all keys from clarigrid.energy
               2. interactive: browser flow → prompt flow fallback
               3. non-interactive: raise ConfigurationError
  PRESENT  → lightweight upstream validation call
  VALID    → cached in-memory, zero latency on repeated connect() calls
  INVALID  → raise InvalidKeyError; never re-trigger auth flow

ClarigGrid SDK key
------------------
CLARIGRID_API_KEY is a user-scoped UUID issued by clarigrid.energy.
When present it is used to fetch ALL provider keys (ENTSO-E, TenneT …)
from the backend in one call, replacing per-provider browser logins.
Set it via env var for CI/headless:  export CLARIGRID_API_KEY=<uuid>

Adding a new provider
---------------------
Add one entry to PROVIDER_AUTH below.  Everything else (auth flow,
wizard, CLI, config store) picks it up automatically.

The distinction between MISSING (triggers auth) and INVALID (raises
immediately, no retry) is a hard requirement of the public API contract.
"""

from __future__ import annotations

import re
import sys
from enum import Enum
from typing import Any

# ── UUID pattern (shared by ENTSO-E, TenneT, and ClarigGrid SDK key) ──────
_UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_UUID_HINT = "UUID format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

# ── ClarigGrid SDK key (meta-key that unlocks all provider keys) ───────────
CLARIGRID_SDK_KEY_ENV = "CLARIGRID_API_KEY"

# ── Provider registry ──────────────────────────────────────────────────────

#: Metadata for every data source that requires an API key.
#: To add a new provider: append an entry here — nothing else needs changing.
PROVIDER_AUTH: dict[str, dict[str, Any]] = {
    "entsoe": {
        "env_var": "ENTSOE_API_KEY",
        "display_name": "ENTSO-E Transparency Platform",
        "registration_url": "https://transparency.entsoe.eu/",
        "registration_instructions": (
            "  1. Register at https://transparency.entsoe.eu/\n"
            "  2. My Account → Security Token → Generate Token\n"
            "  3. Copy the UUID shown"
        ),
        "key_pattern": _UUID_PATTERN,
        "key_format_hint": _UUID_HINT,
    },
    "tennet": {
        "env_var": "TENNET_API_KEY",
        "display_name": "TenneT Transparency Portal",
        "registration_url": "https://transparency.tennet.eu/",
        "registration_instructions": (
            "  1. Register at https://transparency.tennet.eu/\n"
            "  2. My Account → API Access → Generate Token"
        ),
        "key_pattern": _UUID_PATTERN,
        "key_format_hint": _UUID_HINT,
    },
}

#: Providers that never need an API key — auth check is a no-op.
FREE_PROVIDERS: frozenset[str] = frozenset(
    {"smard", "elia", "neso", "elexon", "entsog", "openmeteo"}
)

#: Base URL for the ClarigGrid web app (browser flow + token exchange).
#: Override with CLARIGRID_APP_URL for local dev or staging.
import os as _os
_APP_BASE_URL = _os.environ.get("CLARIGRID_APP_URL", "https://clarigrid.energy").rstrip("/")
_TOKEN_EXCHANGE_URL = f"{_APP_BASE_URL}/api/cli/exchange-token"


# ── In-memory validation cache ─────────────────────────────────────────────

# provider_name → True (VALID) | False (INVALID).
# Populated after a successful/failed upstream validation call.
# Cleared only on process restart — never re-validate in the same session.
_valid_cache: dict[str, bool] = {}


class KeyState(Enum):
    MISSING = "missing"
    PRESENT = "present"
    VALID = "valid"
    INVALID = "invalid"


# ── Key state resolution ───────────────────────────────────────────────────

def get_key_state(source: str) -> KeyState:
    """Determine the current key state for *source* without network calls."""
    # Check in-memory cache first (VALID / INVALID).
    if source in _valid_cache:
        return KeyState.VALID if _valid_cache[source] else KeyState.INVALID

    cfg = PROVIDER_AUTH.get(source)
    if cfg is None:
        # Unrecognised or free provider — treat as already valid.
        return KeyState.VALID

    from clarigrid._keystore import get_key
    key = get_key(cfg["env_var"])
    return KeyState.PRESENT if key is not None else KeyState.MISSING


# ── Upstream validation (lightweight test call) ────────────────────────────

def _validate_key(source: str, key: str) -> bool:
    """Return True if *key* is accepted by the upstream API, False if rejected.

    Network errors are treated as inconclusive (return True — try later).
    The key value must not appear in any exception message.
    """
    if source == "entsoe":
        return _validate_entsoe(key)
    if source == "tennet":
        return _validate_tennet(key)
    return True  # Unknown provider — assume valid.


def _validate_entsoe(key: str) -> bool:
    try:
        import requests
        resp = requests.get(
            "https://web-api.tp.entsoe.eu/api",
            params={
                "securityToken": key,
                "documentType": "A75",
                "processType": "A16",
                "in_Domain": "10YBE----------2",
                "periodStart": "202501010000",
                "periodEnd": "202501010100",
            },
            timeout=10,
        )
        return resp.status_code not in (401, 403)
    except Exception:
        return True  # Network issue — assume valid, will fail on real request.


def _validate_tennet(key: str) -> bool:
    # Upstream API validation not yet implemented (provider in progress).
    # Validate UUID format at minimum — rejects obviously wrong keys.
    return bool(re.match(_UUID_PATTERN, key, re.IGNORECASE))


# ── Main entry point ───────────────────────────────────────────────────────

# ── ClarigGrid SDK key: fetch all provider keys in one call ───────────────

def fetch_all_provider_keys(sdk_key: str) -> dict[str, str]:
    """Call clarigrid.energy with *sdk_key* and return {service: api_key} map.

    Saves all returned keys to ~/.config/clarigrid/.env so subsequent
    get_key() calls resolve without network access.

    Returns the raw {service_name: key_value} dict (may be empty if no
    provider keys are stored in the user's account yet).
    """
    import requests

    from clarigrid._keystore import write_config

    try:
        resp = requests.get(
            f"{_APP_BASE_URL}/api/cli/fetch-keys",
            headers={"Authorization": f"Bearer {sdk_key}"},
            timeout=15,
        )
        resp.raise_for_status()
        service_keys: dict[str, str] = resp.json()  # {"entsoe": "uuid", ...}
    except Exception as exc:
        # Non-fatal: fall through to per-provider auth flows.
        # Don't log exc — it may contain the SDK key in headers repr.
        return {}

    # Map service names → env var names and persist.
    env_vars: dict[str, str] = {
        cfg["env_var"]: service_keys[src]
        for src, cfg in PROVIDER_AUTH.items()
        if src in service_keys
    }
    if env_vars:
        write_config(env_vars)

    return service_keys


def _try_sdk_key_fetch(source: str) -> bool:
    """Try fetching all keys via CLARIGRID_API_KEY.  Returns True if *source* key found."""
    from clarigrid._keystore import get_key

    sdk_key = get_key(CLARIGRID_SDK_KEY_ENV)
    if not sdk_key:
        return False

    service_keys = fetch_all_provider_keys(sdk_key)
    return source in service_keys


# ── Main entry point ───────────────────────────────────────────────────────

def ensure_authenticated(source: str) -> None:
    """Ensure *source* has a confirmed-valid key before connecting.

    Resolution order for MISSING state:
      1. CLARIGRID_API_KEY present → fetch all provider keys from clarigrid.energy
      2. Interactive terminal → browser flow → prompt flow fallback
      3. Non-interactive → raise ConfigurationError

    Never loops on INVALID state — raises immediately.
    No-op for free providers and unknown sources.
    """
    if source in FREE_PROVIDERS:
        return

    if source not in PROVIDER_AUTH:
        return  # Unrecognised source — no auth required.

    state = get_key_state(source)

    if state == KeyState.VALID:
        return  # In-memory cache hit — zero latency.

    if state == KeyState.INVALID:
        cfg = PROVIDER_AUTH[source]
        from clarigrid.core.exceptions import InvalidKeyError
        raise InvalidKeyError(source, cfg["env_var"])

    if state == KeyState.MISSING:
        # Step 1: try fetching via ClarigGrid SDK key (works in CI too).
        if _try_sdk_key_fetch(source):
            state = get_key_state(source)
        # Step 2: still missing → interactive/non-interactive auth flow.
        if state == KeyState.MISSING:
            _handle_missing(source)
            state = get_key_state(source)
        # Step 3: still nothing → give up.
        if state == KeyState.MISSING:
            cfg = PROVIDER_AUTH[source]
            from clarigrid.core.exceptions import ConfigurationError
            raise ConfigurationError(source, cfg)
        # Fall through to validate the newly-provided key.

    # PRESENT — validate against upstream API.
    cfg = PROVIDER_AUTH[source]
    from clarigrid._keystore import get_key
    key = get_key(cfg["env_var"])
    assert key is not None  # guaranteed by state check above

    if _validate_key(source, key):
        _valid_cache[source] = True
    else:
        _valid_cache[source] = False
        from clarigrid.core.exceptions import InvalidKeyError
        raise InvalidKeyError(source, cfg["env_var"])


def _is_interactive() -> bool:
    """Return True when running in an interactive terminal."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def _handle_missing(source: str) -> None:
    """Handle MISSING state: browser flow (interactive) or raise (non-interactive)."""
    cfg = PROVIDER_AUTH[source]

    if not _is_interactive():
        from clarigrid.core.exceptions import ConfigurationError
        raise ConfigurationError(source, cfg)

    print(f"\nNo API key found for '{source}'.")
    print("Opening ClarigGrid in your browser to authenticate...")

    from clarigrid.core.exceptions import AuthTimeoutError, BrowserFlowError, MissingDependencyError

    try:
        run_browser_flow(source)
        return
    except MissingDependencyError:
        print("  (browser flow requires clarigrid[auth] — falling back to manual entry)")
    except AuthTimeoutError:
        print("  Browser setup timed out.")
        print("  Falling back to manual key entry...")
    except BrowserFlowError as exc:
        print(f"  Browser setup failed: {exc}")
        print("  Falling back to manual key entry...")

    run_prompt_flow(source)


# ── Browser flow (delegates to optional module) ────────────────────────────

def run_browser_flow(source: str) -> None:
    """Open browser to authenticate.  Raises MissingDependencyError if unavailable."""
    try:
        from clarigrid._browser_flow import _run_browser_flow
    except ImportError as exc:
        from clarigrid.core.exceptions import MissingDependencyError
        raise MissingDependencyError("clarigrid[auth]", "browser authentication") from exc
    _run_browser_flow(source)


# ── Prompt flow ────────────────────────────────────────────────────────────

def run_prompt_flow(source: str) -> None:
    """Interactively prompt for an API key.  Zero deps beyond stdlib + python-dotenv."""
    import getpass

    from clarigrid._keystore import write_config
    from clarigrid.core.exceptions import ConfigurationError

    cfg = PROVIDER_AUTH[source]
    env_var: str = cfg["env_var"]
    reg_url: str = cfg["registration_url"]
    reg_instructions: str = cfg["registration_instructions"]
    key_hint: str = cfg["key_format_hint"]
    pattern: str | None = cfg.get("key_pattern")

    print(f"\nClarigGrid — Manual setup for '{source}'")
    print("-" * 45)
    print(f"Get your key at: {reg_url}")
    print()
    print(reg_instructions)
    print()

    for attempt in range(3):
        key = getpass.getpass(f"Paste your {env_var} (input hidden): ").strip()
        if not key:
            print("  Empty input — try again.")
            continue
        if pattern and not re.match(pattern, key, re.IGNORECASE):
            remaining = 2 - attempt
            print(f"  Format invalid. Expected {key_hint}.")
            if remaining > 0:
                print(f"  {remaining} attempt(s) remaining.")
            continue
        write_config({env_var: key})
        print(f"\n  Key saved to ~/.config/clarigrid/.env")
        return

    raise ConfigurationError(source, cfg)


# ── Setup wizard ───────────────────────────────────────────────────────────

def run_setup_wizard() -> None:
    """Interactive wizard to configure all key-required data sources."""
    from clarigrid._keystore import get_key
    from clarigrid.core.exceptions import ConfigurationError

    sources = list(PROVIDER_AUTH.keys())

    print("\nClarigGrid Setup Wizard")
    print("=" * 45)
    print("Data sources that require API keys:\n")
    for i, src in enumerate(sources, 1):
        cfg = PROVIDER_AUTH[src]
        print(f"  {i}. {cfg['display_name']}  ({src})")

    print()
    raw = input(
        "Which sources would you like to set up? (e.g. 1,2 or 'all'): "
    ).strip()

    if raw.lower() == "all":
        selected = list(sources)
    else:
        selected = []
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit():
                idx = int(part) - 1
                if 0 <= idx < len(sources):
                    selected.append(sources[idx])

    if not selected:
        print("No sources selected. Run 'clarigrid setup' again later.")
        return

    configured: list[str] = []
    skipped: list[str] = []

    for src in selected:
        cfg = PROVIDER_AUTH[src]
        env_var: str = cfg["env_var"]
        existing = get_key(env_var)

        if existing:
            ans = input(f"\n  '{src}' already configured. Update? [y/N]: ").strip().lower()
            if ans not in ("y", "yes"):
                skipped.append(src)
                continue

        print()
        try:
            try:
                run_browser_flow(src)
            except Exception:
                run_prompt_flow(src)
            configured.append(src)
        except (ConfigurationError, KeyboardInterrupt):
            skipped.append(src)
            print(f"  Skipped '{src}'.")

    print("\n── Summary " + "─" * 35)
    if configured:
        print(f"  Configured : {', '.join(configured)}")
    if skipped:
        print(f"  Skipped    : {', '.join(skipped)}")
    print()
