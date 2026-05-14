"""Shared HTTP client for all providers.

All providers use the module-level session so connection pooling is shared.
"""

from __future__ import annotations

import time
from typing import Any, Generator

import requests

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "clarigrid/0.1.0 (https://github.com/clarigrid/clarigrid)"})

# Timeout applied to every request unless overridden.
DEFAULT_TIMEOUT = 30


def get_json(url: str, params: dict | None = None, timeout: int = DEFAULT_TIMEOUT) -> Any:
    """GET *url*, return parsed JSON. Raises ``requests.HTTPError`` on 4xx/5xx."""
    resp = _SESSION.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def get_text(url: str, params: dict | None = None, timeout: int = DEFAULT_TIMEOUT) -> str:
    resp = _SESSION.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def paginate_offset(
    url: str,
    params: dict,
    data_key: str,
    *,
    limit: int = 10_000,
    limit_param: str = "limit",
    offset_param: str = "offset",
    throttle: float = 0.0,
) -> Generator[list[dict], None, None]:
    """Yield record lists from an offset-paginated endpoint.

    Stops when a page returns fewer records than *limit* (i.e. last page reached).

    Args:
        url: Endpoint URL.
        params: Query params (not mutated).
        data_key: Dot-separated path to the records list in the response JSON
            (e.g. ``"operationalData"`` or ``"result.records"``).
        limit: Page size to request.
        limit_param: Query param name for page size.
        offset_param: Query param name for offset.
        throttle: Sleep seconds between pages (courtesy rate limiting).
    """
    p = {**params, limit_param: limit, offset_param: 0}
    while True:
        data = get_json(url, p)
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
    throttle: float = 0.0,
) -> Generator[list[dict], None, None]:
    """Yield record lists from a page-number-paginated endpoint."""
    p = {**params, size_param: page_size, page_param: 1}
    while True:
        data = get_json(url, p)
        page = _nested_get(data, data_key)
        if not page:
            break
        yield page
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
