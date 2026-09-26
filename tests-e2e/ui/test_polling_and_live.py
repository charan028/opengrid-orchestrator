"""Refresh mechanics per screen (02b S8): Markets and Profitability poll themselves through htmx every
30 s; Control room, Fleet, Dispatch and Health subscribe to `/og/api/stream/*` (02b S7.2) and show a
`live` header badge. Whether a stream *delivers* an update needs the real dev stack -- the fixture server
only holds the connection open -- so that one test is live-stack only."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, Request, expect

from conftest import live_base_url
from screens import LIVE_SCREENS, POLLING_SCREENS, goto_ok

_POLL_TRIGGER = "every 30s"
_SSE_UPDATE_TIMEOUT_MS = 2_000

# Counts SSE `message` events per page without touching `og.sse` itself.
_COUNT_SSE_MESSAGES = """
(() => {
  const Native = window.EventSource;
  window.__ogSseMessages = 0;
  window.EventSource = function (url, init) {
    const source = new Native(url, init);
    source.addEventListener("message", () => { window.__ogSseMessages += 1; });
    return source;
  };
  window.EventSource.prototype = Native.prototype;
})();
"""


@pytest.mark.parametrize(("path", "wrapper"), POLLING_SCREENS)
def test_screen_polls_itself_every_30s(viewer_page: Page, path: str, wrapper: str) -> None:
    goto_ok(viewer_page, path)

    poll = viewer_page.locator(wrapper)
    expect(poll).to_have_attribute("hx-trigger", _POLL_TRIGGER)
    expect(poll).to_have_attribute("hx-get", path)
    expect(viewer_page.locator(".og-header-actions .stale-badge")).to_have_text("poll: 30s")


@pytest.mark.parametrize(("path", "stream"), LIVE_SCREENS)
def test_live_screen_subscribes_to_its_stream(viewer_page: Page, path: str, stream: str) -> None:
    def is_stream_request(request: Request) -> bool:
        return request.url.endswith(f"/og/api/stream/{stream}")

    with viewer_page.expect_request(is_stream_request):
        goto_ok(viewer_page, path)

    expect(viewer_page.locator(".og-header-actions .stale-badge")).to_have_text("live")


@pytest.mark.skipif(not live_base_url(), reason="needs live stack: the fixture server has no streams")
@pytest.mark.parametrize(("path", "_stream"), LIVE_SCREENS)
def test_sse_update_arrives_within_2s(viewer_page: Page, path: str, _stream: str) -> None:
    viewer_page.add_init_script(_COUNT_SSE_MESSAGES)
    goto_ok(viewer_page, path)

    viewer_page.wait_for_function("() => window.__ogSseMessages > 0", timeout=_SSE_UPDATE_TIMEOUT_MS)
