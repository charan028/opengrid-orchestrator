"""The shared alerts component in the browser (owner UX review, R3): grouping, paging, filters, bulk
selection and the one-confirm acknowledgement, plus the posture strip, on both screens."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from screens import BASE_PATH, goto_ok

SCREENS = [(f"{BASE_PATH}/", "control-room"), (f"{BASE_PATH}/health", "health")]


@pytest.mark.parametrize(("path", "pid"), SCREENS)
def test_grouped_paged_and_scrollable(operator_page: Page, path: str, pid: str) -> None:
    goto_ok(operator_page, path)
    panel = operator_page.locator(f"#{pid}-alerts-panel")
    expect(panel.locator(".og-alerts-scroll")).to_be_visible()
    box = panel.locator(".og-alerts-scroll").bounding_box()
    assert box and box["height"] <= 422
    expect(panel.locator("tbody tr")).to_have_count(20)  # page 1 of the grouped list
    expect(panel.get_by_text(re.compile("\u00d7\\d+")).first).to_be_visible()  # repeats collapsed
    panel.locator(".og-alerts-pager button", has_text="2").click()
    panel = operator_page.locator(f"#{pid}-alerts-panel")
    expect(panel.locator('.og-alerts-pager [aria-current="page"]')).to_have_text("2")


@pytest.mark.parametrize(("path", "pid"), SCREENS)
def test_filter_by_severity(operator_page: Page, path: str, pid: str) -> None:
    goto_ok(operator_page, path)
    operator_page.locator(f"#{pid}-alert-sev").select_option("critical")
    panel = operator_page.locator(f"#{pid}-alerts-panel")
    expect(panel.locator("tbody tr").first).to_contain_text("ALR-PROCESS-DOWN")
    expect(panel.locator("tbody")).not_to_contain_text("ALR-SCOPE-CONSERVATIVE")


def test_bulk_select_and_one_confirm_ack(operator_page: Page) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/")
    panel = operator_page.locator("#control-room-alerts-panel")
    ack = panel.locator(".og-alerts-ack-selected")
    expect(ack).to_be_disabled()
    panel.locator(".og-alerts-select-page").click()
    expect(ack).to_be_enabled()
    panel.locator(".og-alerts-select-matching").click()
    expect(panel.locator(".og-alerts-selected-n")).to_have_text(
        panel.locator(".og-alerts-select-matching").get_attribute("data-count") or ""
    )
    ack.click()
    dialog = operator_page.locator("#control-room-ack-dialog .confirm-dialog")
    expect(dialog).to_be_visible()
    dialog.get_by_role("button", name=re.compile("^Acknowledge \\d+")).click()
    expect(operator_page.locator("#control-room-ack-result")).to_contain_text("acknowledged")


def test_posture_strip_links_to_the_filtered_alerts(operator_page: Page) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/")
    strip = operator_page.locator("#control-room-posture-strip")
    expect(strip).to_contain_text("3 scopes conservative (bank-003, bank-007, bank-012)")
    expect(strip).to_contain_text("clears automatically after 3 clean cycles")
    strip.get_by_role("link").click()
    expect(operator_page.locator("#control-room-alert-rule")).to_have_value("ALR-SCOPE-CONSERVATIVE")


def test_viewer_sees_no_selection(viewer_page: Page) -> None:
    goto_ok(viewer_page, f"{BASE_PATH}/health")
    expect(viewer_page.locator("#health-alerts-panel .og-alert-select")).to_have_count(0)
    expect(viewer_page.locator("#health-alerts-panel .og-alerts-ack-selected")).to_have_count(0)
