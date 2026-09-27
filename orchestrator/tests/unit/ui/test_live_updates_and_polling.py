"""Static template-source checks for BUILD.md code-review round item 2 / blocker 4:

* Control room, Fleet, Health, Dispatch must have real `og.sse(...)` handlers that update their own
  widgets from the JSON payload, not the original no-op stub that only flipped a "live" badge.
* Markets and Profitability must poll themselves via `hx-trigger="every 30s"` (htmx-native), not a
  one-shot `window.setTimeout` (markets.html) or no polling mechanism at all (profitability.html).

Following `test_static_staleness.py`'s convention: these assert on the rendered template *source* text
rather than driving a browser, since there is no JS test runner in this repo (BUILD.md task brief)."""

from __future__ import annotations

from pathlib import Path

import pytest

TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "src" / "opengrid" / "ui" / "templates"

_SSE_SCREENS: tuple[tuple[str, str], ...] = (
    ("story.html", "data.reserve_breach_count"),
    ("control_room.html", "data.fleet_mw"),
    ("fleet.html", "data.hubs"),
    ("health.html", "data.hub_health_counts"),
    ("health.html", "data.cycle_latency"),  # #43 B7: the latency panel used to be static "no samples"
    ("dispatch.html", "data.opportunities"),
)


@pytest.mark.parametrize(("screen", "field_ref"), _SSE_SCREENS)
def test_sse_handler_references_real_payload_fields(screen: str, field_ref: str) -> None:
    text = (TEMPLATES_DIR / screen).read_text(encoding="utf-8")
    assert field_ref in text, (
        f"{screen}'s og.sse(...) handler must read {field_ref} off the stream payload and update a "
        "widget with it, not just flip a 'live' badge (BUILD.md code-review round blocker 4)"
    )


@pytest.mark.parametrize("screen", ["markets.html", "profitability.html"])
def test_screen_polls_itself_every_30s_via_htmx(screen: str) -> None:
    text = (TEMPLATES_DIR / screen).read_text(encoding="utf-8")
    assert 'hx-trigger="every 30s"' in text
    # the original bug was a one-shot `window.setTimeout(...)` (markets.html) refetching the page a
    # single time -- no actual JS timer call should remain, only htmx's own polling trigger above.
    assert "window.setTimeout(" not in text


def test_markets_series_fragment_chart_init_is_not_dom_content_loaded_gated() -> None:
    # DOMContentLoaded only ever fires once for the whole document; a script wrapped in a listener for
    # it would never re-run its og.chart(...) call after the 30s poll swaps this fragment back in.
    text = (TEMPLATES_DIR / "markets_series_fragment.html").read_text(encoding="utf-8")
    assert "og.chart(" in text
    assert 'addEventListener("DOMContentLoaded"' not in text
