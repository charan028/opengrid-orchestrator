"""The degraded-mode banner's live path (`og.renderDegradedBanner`, driven by the health/control-room
streams): server labels, unknown codes kept, hidden when nothing is degraded, untouched by a frame without
the field."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from screens import goto_ok


@pytest.mark.parametrize(("path", "banner_id"), [("/og/health", "health"), ("/og/", "control-room")])
def test_stream_frame_updates_the_degraded_banner(operator_page: Page, path: str, banner_id: str) -> None:
    goto_ok(operator_page, path)
    banner = operator_page.locator(f"#{banner_id}-degraded-banner")
    expect(banner).to_be_hidden()
    expect(banner).to_have_attribute("role", "alert")

    render = (
        f"(modes) => og.renderDegradedBanner(document.getElementById('{banner_id}-degraded-banner'), modes)"
    )
    operator_page.evaluate(render, ["NO_NEW_COMMITMENTS", "HOLD", "SOMETHING_NEW"])
    expect(banner).to_be_visible()
    expect(banner).to_have_text("Degraded mode: Feed stale + Guardian down + SOMETHING_NEW")

    operator_page.evaluate(render, None)  # a frame without `degraded_modes` leaves the banner alone
    expect(banner).to_be_visible()

    operator_page.evaluate(render, [])
    expect(banner).to_be_hidden()
