"""Shared HTTP client for all providers.

All providers use the module-level session so connection pooling is shared.

Retry behaviour
---------------
``get_json`` / ``get_bytes`` / ``get_text`` automatically retry on:

- **HTTP 429** (rate limit): waits ``Retry-After`` seconds if the header is
  present, otherwise uses exponential back-off starting at 2 s.
- **HTTP 5xx** (server errors): exponential back-off (2 s, 4 s, 8 s …).
- **Connection errors** (network blips): same exponential back-off.

After ``max_retries`` attempts a ``RateLimitError`` (for 429) or
``ProviderUnavailableError`` (for 5xx / network) is raised.
"""

from __future__ import annotations

import time
from typing import Any, Generator

import requests

from clarigrid.core.exceptions import ProviderUnavailableError, RateLimitError

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "clarigrid/0.2.0 (https://github.com/clarigrid/clarigrid)"})

# Applied to every request unless overridden.
DEFAULT_TIMEOUT = 30

# Default retry attempts for transient errors.
_DEFAULT_RETRIES = 3


# ── Core request helper ────────────────────────────────────────────────────

def _get(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = _DEFAULT_RETRIES,
) -> requests.Response:
    """GET *url* with exponential back-off retry.

    Raises:
        RateLimitError: 429 persisted after all retries.
        ProviderUnavailableError: 5xx or connection error persisted after
            all retries.
        requests.HTTPError: Other 4xx errors (not retried).
    """
    delay = 2.0
    last_exc: Exception | None = None

    for attempt in range(max_retries):
        try:
            resp = _SESSION.get(url, params=params, headers=headers, timeout=timeout)
        except requests.ConnectionError as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise ProviderUnavailableError(
                f"Connection failed after {max_retries} attempts: {url}"
            ) from exc

        if resp.status_code == 429:
            if attempt < max_retries - 1:
                wait = float(resp.headers.get("Retry-After", delay))
                time.sleep(wait)
                delay = max(delay * 2, wait)
                continue
            raise RateLimitError(
                f"Rate limited by {url} after {max_retries} attempts. "
                "Try again later or reduce request frequency."
            )

        if resp.status_code >= 500:
            if attempt < max_retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise ProviderUnavailableError(
                f"Server error {resp.status_code} from {url} after {max_retries} attempts."
            )

        # 4xx (excluding 429) — not retried, raise immediately.
        resp.raise_for_status()
        return resp

    # Should not be reached, but satisfy type checker.
    if last_exc:
        raise ProviderUnavailableError(str(last_exc)) from last_exc
    raise ProviderUnavailableError(f"Request failed: {url}")


# ── Public functions ───────────────────────────────────────────────────────

def get_json(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = _DEFAULT_RETRIES,
) -> Any:
    """GET *url*, return parsed JSON."""
    return _get(url, params=params, headers=headers, timeout=timeout,
                max_retries=max_retries).json()


def get_bytes(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = _DEFAULT_RETRIES,
) -> bytes:
    """GET *url*, return raw bytes (used for ZIP / binary file downloads)."""
    return _get(url, params=params, headers=headers, timeout=timeout,
                max_retries=max_retries).content


def get_text(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = _DEFAULT_RETRIES,
) -> str:
    """GET *url*, return response text."""
    return _get(url, params=params, headers=headers, timeout=timeout,
                max_retries=max_retries).text


def paginate_offset(
    url: str,
    params: dict,
    data_key: str,
    *,
    limit: int = 10_000,
    limit_param: str = "limit",
    offset_param: str = "offset",
    throttle: float = 0.0,
    headers: dict | None = None,
) -> Generator[list[dict], None, None]:
    """Yield record lists from an offset-paginated endpoint.

    Stops when a page returns fewer records than *limit* (last page reached).

    Args:
        url: Endpoint URL.
        params: Query params (not mutated).
        data_key: Dot-separated path to the records list in the response JSON
            (e.g. ``"operationalData"`` or ``"result.records"``).
        limit: Page size to request.
        limit_param: Query param name for page size.
        offset_param: Query param name for offset.
        throttle: Sleep seconds between pages (courtesy rate limiting).
        headers: Optional extra request headers.
    """
    p = {**params, limit_param: limit, offset_param: 0}
    while True:
        data = get_json(url, p, headers=headers)
        page = _nested_get(data, data_key)
        if not page:
            break
        yield page
        if len(page) < limit:
            break
        p[offset_param] += limit
        if throttle:
            time.sleep(throttle)


def paginate_pages(
    url: str,
    params: dict,
    data_key: str,
    *,
    page_param: str = "page",
    size_param: str = "pageSize",
    page_size: int = 100,
    start_page: int = 1,
    total_pages_key: str | None = None,
    throttle: float = 0.0,
    headers: dict | None = None,
) -> Generator[list[dict], None, None]:
    """Yield record lists from a page-number-paginated endpoint.

    Args:
        start_page: First page number (1 for most APIs, 0 for Spring Boot).
        total_pages_key: JSON key for total page count — enables early stop
            (e.g. ``"totalPages"`` for Spring Boot pagination wrappers).
    """
    p = {**params, size_param: page_size, page_param: start_page}
    while True:
        data = get_json(url, p, headers=headers)
        page = _nested_get(data, data_key)
        if not page:
            break
        yield page
        if total_pages_key:
            total = data.get(total_pages_key, 1) if isinstance(data, dict) else 1
            if p[page_param] + 1 - start_page >= total:
                break
        if len(page) < page_size:
            break
        p[page_param] += 1
        if throttle:
            time.sleep(throttle)


def _nested_get(data: Any, key: str) -> list:
    """Traverse a dot-separated key path through nested dicts."""
    for part in key.split("."):
        if not isinstance(data, dict):
            return []
        data = data.get(part, [])
    return data if isinstance(data, list) else []
