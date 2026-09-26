"""Role gating (02b S7, `opengrid.ui.role`): a viewer never sees a write action -- scoped safe stop,
manual command, alert acknowledge -- and the server rejects the write even if the form were forged; an
operator sees them all. The chain-verify button is a read-only audit action, visible to both roles."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from screens import BASE_PATH, goto_ok

# (path, selector of a write action on that screen)
WRITE_ACTIONS: tuple[tuple[str, str], ...] = (
    (f"{BASE_PATH}/fleet", "#safestop-propose-form"),
    (f"{BASE_PATH}/fleet", "#manual-command-form"),
    (f"{BASE_PATH}/", "#control-room-alerts-panel .og-alerts-ack-selected"),
    (f"{BASE_PATH}/health", "#health-alerts-panel .og-alerts-ack-selected"),
    (f"{BASE_PATH}/dispatch", "#as-deployment-form"),
)


@pytest.mark.parametrize(("path", "selector"), WRITE_ACTIONS)
def test_viewer_does_not_see_write_action(viewer_page: Page, path: str, selector: str) -> None:
    goto_ok(viewer_page, path)

    expect(viewer_page.locator(selector)).to_have_count(0)
    expect(viewer_page.locator(".og-nav-role")).to_contain_text("Role: viewer")


@pytest.mark.parametrize(("path", "selector"), WRITE_ACTIONS)
def test_operator_sees_write_action(operator_page: Page, path: str, selector: str) -> None:
    goto_ok(operator_page, path)

    expect(operator_page.locator(selector)).to_have_count(1)
    expect(operator_page.locator(".og-nav-role")).to_contain_text("Role: operator")


def test_viewer_write_is_rejected_server_side(viewer_page: Page) -> None:
    """Hiding the form is cosmetic; the UI route itself must refuse a viewer's POST."""
    response = viewer_page.request.post(
        f"{BASE_PATH}/fleet/command/propose",
        form={"bank_id": "bank-01", "hub_id": "", "p_kw_setpoint": "5.0", "reason": "forged"},
    )
    assert response.status == 403


def test_chain_verify_is_read_only_and_visible_to_both_roles(viewer_page: Page, operator_page: Page) -> None:
    for page in (viewer_page, operator_page):
        goto_ok(page, f"{BASE_PATH}/billing")
        expect(page.get_by_role("button", name="Run chain verify")).to_be_visible()
