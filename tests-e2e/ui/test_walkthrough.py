"""Scripted walkthrough of all 7 screens (02b S8) in a real browser: each loads as a 200, carries the
expected `<h1>`, and every value shows its age -- the runtime mirror of
`orchestrator/tests/unit/ui/test_static_staleness.py`: every `[data-since]` badge is ticked by `og.js`
into a concrete "age: Ns/m/h" (never left at "age: unknown" when a timestamp is present) and every KPI
tile carries a stale badge."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from screens import BASE_PATH, SCREENS, goto_ok

_AGE_TEXT = re.compile(r"^age: \d+[smh]$")
_DATED_BADGE = '.stale-badge[data-since]:not([data-since=""])'


@pytest.mark.parametrize(("path", "h1"), SCREENS)
def test_screen_loads_with_header(viewer_page: Page, path: str, h1: str) -> None:
    goto_ok(viewer_page, path)

    expect(viewer_page.locator("h1")).to_have_text(h1)
    expect(viewer_page.locator(".og-nav a[aria-current='page']")).to_have_attribute("href", path)


@pytest.mark.parametrize(("path", "_h1"), SCREENS)
def test_every_value_shows_its_age(viewer_page: Page, path: str, _h1: str) -> None:
    goto_ok(viewer_page, path)

    dated = viewer_page.locator(_DATED_BADGE)
    expect(dated.first).to_be_attached()
    for index in range(dated.count()):
        expect(dated.nth(index)).to_have_text(_AGE_TEXT)
    # a KPI tile without a stale badge would be a value with no age
    expect(viewer_page.locator(".kpi-tile:not(:has(.stale-badge))")).to_have_count(0)


def test_fleet_hub_drilldown_shows_its_age(viewer_page: Page) -> None:
    goto_ok(viewer_page, f"{BASE_PATH}/fleet")

    viewer_page.locator("tr.clickable-row").first.click()

    drilldown = viewer_page.locator("#hub-drilldown")
    expect(drilldown).to_be_visible()
    expect(drilldown.locator("h2")).to_contain_text("Hub ")
    expect(drilldown.locator(_DATED_BADGE)).to_have_text(_AGE_TEXT)
