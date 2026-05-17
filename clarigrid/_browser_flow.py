"""Browser-based CLI authentication flow.

Opens the ClarigGrid web app for Supabase login, receives a one-time token
via localhost callback, exchanges it for the actual API key.

All dependencies are stdlib + requests (already a core dep) — no extra
packages required. The [auth] optional group exists for CLI tooling (click).

Raised by: clarigrid._auth.run_browser_flow()
"""

from __future__ import annotations

import http.server
import random
import socket
import threading
import urllib.parse
from typing import Any

import requests

from clarigrid._auth import PROVIDER_AUTH, _TOKEN_EXCHANGE_URL
from clarigrid._keystore import write_config
from clarigrid.core.exceptions import AuthTimeoutError, BrowserFlowError

_TIMEOUT_SECONDS = 120


# ── Port discovery ─────────────────────────────────────────────────────────

def _find_free_port() -> int:
    """Pick a random available port in the ephemeral range 49152–65535."""
    for _ in range(30):
        port = random.randint(49152, 65535)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("localhost", port))
                return port
            except OSError:
                continue
    raise BrowserFlowError("Could not bind a free port in range 49152–65535.")


# ── Callback HTTP handler ──────────────────────────────────────────────────

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Single-request handler: stores query params, signals the wait event."""

    # Class-level state shared with the waiting thread.
    result: dict[str, str] | None = None
    _event: threading.Event = threading.Event()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        _CallbackHandler.result = params

        # Send a success page so the user sees a clean browser message.
        body = (
            b"<html><head><title>ClarigGrid</title></head><body>"
            b"<h2>Authentication complete!</h2>"
            b"<p>You may close this tab and return to your terminal.</p>"
            b"</body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        _CallbackHandler._event.set()

    def log_message(self, *_args: Any) -> None:
        """Suppress server access logs."""


# ── Main browser flow ──────────────────────────────────────────────────────

def _run_browser_flow(source: str) -> None:
    """Full browser-based auth flow.  Called from clarigrid._auth.run_browser_flow()."""
    import webbrowser

    from clarigrid._auth import _APP_BASE_URL

    cfg = PROVIDER_AUTH.get(source)
    if cfg is None:
        raise BrowserFlowError(f"No auth config registered for source '{source}'.")

    port = _find_free_port()
    callback_url = f"http://localhost:{port}/callback"
    login_url = (
        f"{_APP_BASE_URL}/auth/cli"
        f"?source={urllib.parse.quote(source)}"
        f"&callback={urllib.parse.quote(callback_url)}"
    )

    # Reset class-level state before each run.
    _CallbackHandler.result = None
    _CallbackHandler._event.clear()

    server = http.server.HTTPServer(("localhost", port), _CallbackHandler)
    # handle_request() blocks until one request is served — run in daemon thread.
    server_thread = threading.Thread(target=server.handle_request, daemon=True)
    server_thread.start()

    print(f"  Opening browser...")
    opened = webbrowser.open(login_url)
    if not opened:
        print(f"  Could not open browser automatically.")
        print(f"  Open this URL manually: {login_url}")

    print(f"  Waiting up to {_TIMEOUT_SECONDS}s for authentication...")

    completed = _CallbackHandler._event.wait(timeout=_TIMEOUT_SECONDS)
    server.server_close()

    if not completed:
        raise AuthTimeoutError(
            f"Browser authentication timed out after {_TIMEOUT_SECONDS} seconds."
        )

    params = _CallbackHandler.result or {}

    if "error" in params:
        raise BrowserFlowError(
            f"Authentication server returned error: {params['error']}"
        )

    token = params.get("token")
    if not token:
        raise BrowserFlowError(
            "Callback received but no 'token' parameter found in redirect URL."
        )

    # Exchange one-time token for the user's ClarigGrid SDK key (UUID).
    try:
        resp = requests.post(
            _TOKEN_EXCHANGE_URL,
            json={"token": token},
            timeout=15,
        )
        resp.raise_for_status()
        data: dict[str, str] = resp.json()
    except requests.HTTPError as exc:
        raise BrowserFlowError(
            f"Token exchange failed ({exc.response.status_code}): "
            f"{exc.response.text[:200]}"
        ) from exc
    except Exception as exc:
        raise BrowserFlowError(f"Token exchange request failed: {exc}") from exc

    sdk_key = data.get("sdk_key")
    if not sdk_key:
        raise BrowserFlowError(
            "Token exchange response did not contain an 'sdk_key' field."
        )

    # Persist the ClarigGrid SDK key — acts as master credential for all providers.
    from clarigrid._auth import CLARIGRID_SDK_KEY_ENV, fetch_all_provider_keys
    write_config({CLARIGRID_SDK_KEY_ENV: sdk_key})
    print(f"  ClarigGrid SDK key saved.")

    # Immediately fetch all provider keys the user has stored in their account.
    print(f"  Fetching your provider keys from clarigrid.energy...")
    service_keys = fetch_all_provider_keys(sdk_key)
    if service_keys:
        print(f"  Retrieved keys for: {', '.join(service_keys.keys())}")
    else:
        print(f"  No provider keys stored yet. Add them at clarigrid.energy/settings/api-keys")
