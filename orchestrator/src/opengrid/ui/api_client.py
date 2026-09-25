"""HTTP client the UI's own screen routes use to consume `opengrid.api`'s REST endpoints (02b S7) for
first-paint server rendering. Owner: ui-a (BUILD.md S4).

The UI and API are built in parallel by separate agents (BUILD.md task brief: "consume them by path per
02b S7"); this module is the only place that boundary is crossed, and it is a plain HTTP call to the
running `og-api` process, never an import of `opengrid.api` internals. Tests monkeypatch `get_json`
directly and feed it recorded JSON fixtures instead of a live API (BUILD.md task brief).

Live, per-second updates are the browser's job (SSE via `og.sse()`, static/og.js) -- this module is only
used for the initial HTMX GET / server-rendered paint of each screen (02b S8 "static shell + first paint
is a normal HTMX GET").
"""

from __future__ import annotations

import os
from typing import Any

import httpx

_DEFAULT_BASE_URL = "http://127.0.0.1:8080"
_TIMEOUT_S = 3.0
_ENV_BASE_URL = "OG_API_BASE_URL"


class ApiUnavailable(Exception):  # noqa: N818 -- shared symbol name; ui-b's screens already import it
    """Raised when the API cannot be reached or returns a non-2xx status. Routes catch this and render
    the screen degraded (missing widgets, a visible banner) rather than a 500 -- an operator console must
    stay usable when one dependency is slow or down (02b S6.5 degraded-mode principle, applied to the
    UI's own read path)."""


def api_base_url() -> str:
    """Base URL of the `og-api` process. Overridable via `OG_API_BASE_URL` for tests/dev; defaults to the
    loopback address `[api].bind_host`/`bind_port` resolve to in production (02b S1.4)."""
    return os.environ.get(_ENV_BASE_URL, _DEFAULT_BASE_URL)


async def get_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
    """GET `path` (e.g. `/og/api/health`) from the API and return the parsed JSON body.

    Raises `ApiUnavailable` on any transport error, timeout, or non-2xx response -- callers must treat
    that as "no data yet," never let it propagate to a bare 500.
    """
    url = f"{api_base_url()}{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        raise ApiUnavailable(f"GET {path} failed: {exc}") from exc
