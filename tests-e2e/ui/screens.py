"""Shared screen catalogue for the UI end-to-end suite (02b S8: the 7 screens at their fixed paths,
`opengrid.ui.templating.NAV_SCREENS`) plus the one navigation helper every test uses."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

BASE_PATH = "/og"

# (path, expected <h1> text) -- `header_title` block of each screen template.
SCREENS: tuple[tuple[str, str], ...] = (
    (f"{BASE_PATH}/", "Control room"),
    (f"{BASE_PATH}/fleet", "Fleet monitoring & control"),
    (f"{BASE_PATH}/dispatch", "Dispatch & commitments"),
    (f"{BASE_PATH}/markets", "Markets & feeds"),
    (f"{BASE_PATH}/health", "Health"),
    (f"{BASE_PATH}/profitability", "Profitability"),
    (f"{BASE_PATH}/billing", "Billing & audit"),
)

# (path, SSE stream name) -- screens that subscribe to `/og/api/stream/<name>` via `og.sse` (02b S7.2).
LIVE_SCREENS: tuple[tuple[str, str], ...] = (
    (f"{BASE_PATH}/", "control-room"),
    (f"{BASE_PATH}/fleet", "health"),  # api serves no fleet stream (NEEDS_FROM_OTHER_OWNERS.md)
    (f"{BASE_PATH}/dispatch", "dispatch"),
    (f"{BASE_PATH}/health", "health"),
)

# (path, poll wrapper selector) -- screens that re-fetch themselves via htmx every 30 s instead of SSE.
POLLING_SCREENS: tuple[tuple[str, str], ...] = (
    (f"{BASE_PATH}/markets", "#markets-poll-wrapper"),
    (f"{BASE_PATH}/profitability", "#profitability-poll-wrapper"),
)


def goto_ok(page: Page, path: str) -> None:
    """Navigate to `path` (relative to the context's `base_url`); fails unless the document itself was a 200."""
    response = page.goto(path)
    if response is None or response.status != 200:
        status = response.status if response is not None else "no response"
        pytest.fail(f"GET {path} -> {status}")
