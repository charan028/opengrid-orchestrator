"""The notification centre in the browser (owner UX review R3.1): the header bell with a severity-coloured
count, the popover (Safety / Degraded modes / Alerts) by mouse and keyboard, nothing stacked above the map,
and the live degraded-mode hook that refreshes the bell."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from screens import BASE_PATH, goto_ok


@pytest.mark.parametrize("path", [f"{BASE_PATH}/", f"{BASE_PATH}/health", f"{BASE_PATH}/dispatch"])
def test_bell_shows_the_count_coloured_by_severity(operator_page: Page, path: str) -> None:
    goto_ok(operator_page, path)
    bell = operator_page.locator("#og-notify-bell")
    expect(bell).to_have_attribute("data-severity", "critical")  # the fixture has critical process alerts
    expect(bell).to_have_class(re.compile(r"\bsev-critical\b"))
    count = int(bell.get_attribute("data-count") or "0")
    assert count > 0
    expect(bell.locator(".og-notify-badge")).to_have_text(str(count) if count < 100 else "99+")
    expect(bell).to_have_attribute(
        "aria-label", re.compile(rf"^Notifications: {count}, highest severity critical")
    )


def test_popover_groups_and_links(operator_page: Page) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/")
    panel = operator_page.locator("#og-notify-panel")
    expect(panel).to_be_hidden()
    operator_page.locator("#og-notify-bell").click()
    expect(panel).to_be_visible()
    expect(panel.locator("#og-notify-safety")).to_contain_text("3 scopes held conservative")
    expect(panel.locator("#og-notify-safety")).to_contain_text("bank-003, bank-007, bank-012")
    alerts = panel.locator("#og-notify-alerts")
    expect(alerts).to_contain_text("ALR-PROCESS-DOWN")
    expect(alerts.locator(".og-notify-item")).to_have_count(8)
    expect(alerts.locator(".og-notify-more")).to_contain_text("more")
    expect(alerts.get_by_role("button", name=re.compile("^Acknowledge"))).to_have_count(8)
    panel.locator("#og-notify-safety").get_by_role("link", name="3 scopes held conservative").click()
    expect(operator_page).to_have_url(re.compile(r"/og/health\?alert_rule=ALR-SCOPE-CONSERVATIVE"))
    expect(operator_page.locator("#health-alert-rule")).to_have_value("ALR-SCOPE-CONSERVATIVE")


def test_keyboard_opens_and_escape_closes(operator_page: Page) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/health")
    bell = operator_page.locator("#og-notify-bell")
    expect(bell).to_be_enabled()
    bell.focus()
    operator_page.keyboard.press("Enter")
    panel = operator_page.locator("#og-notify-panel")
    expect(panel).to_be_visible()
    operator_page.keyboard.press("Escape")
    expect(panel).to_be_hidden()


def test_viewer_popover_has_no_actions(viewer_page: Page) -> None:
    goto_ok(viewer_page, f"{BASE_PATH}/")
    viewer_page.locator("#og-notify-bell").click()
    panel = viewer_page.locator("#og-notify-panel")
    expect(panel).to_be_visible()
    expect(panel.locator(".og-notify-action")).to_have_count(0)


@pytest.mark.parametrize(
    ("path", "first"), [(f"{BASE_PATH}/", ("og-story", "og-promises")), (f"{BASE_PATH}/health", ())]
)
def test_nothing_stacks_above_the_content(operator_page: Page, path: str, first: tuple[str, ...]) -> None:
    goto_ok(operator_page, path)
    expect(operator_page.locator("#og-notify-bell")).to_be_enabled()
    for gone in (".og-degraded-banner", ".og-guardian-attention", ".og-posture-strip"):
        expect(operator_page.locator(gone)).to_have_count(0)
    expect(operator_page.locator("#og-critical-strip")).to_be_hidden()  # no active critical state here
    first_class = operator_page.locator(".og-content > *").first.get_attribute("class") or ""
    assert "notice" not in first_class and "banner" not in first_class
    if first:  # Control room: the fleet headline / KPI panel, then the map
        assert any(name in first_class for name in first), first_class


def test_a_changed_degraded_mode_refreshes_the_bell(operator_page: Page) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/")
    expect(operator_page.locator("#og-notify-bell")).to_be_enabled()
    assert operator_page.evaluate("og.noteDegradedModes(['HOLD'])") in (True, False)  # first frame: baseline
    with operator_page.expect_response(re.compile(r"/og/alerts/notifications")):
        assert operator_page.evaluate("og.noteDegradedModes([])") is True
    assert operator_page.evaluate("og.noteDegradedModes([])") is False  # unchanged: no refetch
    assert operator_page.evaluate("og.noteDegradedModes(undefined)") is False  # a frame without the field
