"""The Fleet screen's two-step flows (02b S7.3, `_partials/confirm_dialog.html`) with the operator role:
propose -> dialog opens already focused on Cancel -> Tab reaches the confirm button -> Escape closes it
and hands focus back to the button that triggered it -> a confirm renders the fixture's result badge.
The confirm step itself is fixture-only: against a live stack it would really send a command / stop."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from playwright.sync_api import Locator, Page, expect

from conftest import live_base_url
from screens import BASE_PATH, goto_ok


@dataclass(frozen=True)
class Flow:
    form: str
    fields: tuple[tuple[str, str], ...]
    trigger: str
    propose_result: str
    confirm: str
    confirm_result: str
    fixture_summary: str
    fixture_badge: str


COMMAND = Flow(
    form="#manual-command-form",
    fields=(("Bank id", "bank-01"), ("Setpoint (kW)", "5.0"), ("Reason", "load test")),
    trigger="Propose (step 1 of 2)",
    propose_result="#command-propose-result",
    confirm="Send command",
    confirm_result="#command-confirm-result",
    fixture_summary="Set bank-01 to 5.0 kW (load test)",
    fixture_badge="RAMPING",
)
SAFESTOP = Flow(
    form="#safestop-propose-form",
    fields=(("Reason", "planned maintenance"),),
    trigger="Propose safe stop (step 1 of 2)",
    propose_result="#safestop-propose-result",
    confirm="Engage safe stop",
    confirm_result="#safestop-confirm-result",
    fixture_summary="Engage safe stop on FLEET (planned maintenance)",
    fixture_badge="ENGAGED",
)
FLOWS = pytest.mark.parametrize("flow", [COMMAND, SAFESTOP], ids=["command", "safestop"])


def _propose(page: Page, flow: Flow) -> tuple[Locator, Locator]:
    """Fill and submit step 1; returns (trigger button, open dialog)."""
    form = page.locator(flow.form)
    for label, value in flow.fields:
        form.get_by_label(label, exact=True).fill(value)
    trigger = form.get_by_role("button", name=flow.trigger)
    trigger.click()
    dialog = page.locator(f"{flow.propose_result} .confirm-dialog")
    expect(dialog).to_be_visible()
    return trigger, dialog


@FLOWS
def test_dialog_opens_with_focus_on_cancel_and_tab_reaches_confirm(operator_page: Page, flow: Flow) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/fleet")

    _trigger, dialog = _propose(operator_page, flow)

    if not live_base_url():
        expect(dialog).to_contain_text(flow.fixture_summary)
    expect(dialog).to_contain_text("Expires in")
    expect(dialog.get_by_role("button", name="Cancel")).to_be_focused()
    operator_page.keyboard.press("Tab")
    expect(dialog.get_by_role("button", name=flow.confirm)).to_be_focused()
    expect(dialog.get_by_role("button", name=flow.confirm)).to_be_enabled()


@FLOWS
def test_escape_closes_dialog_and_returns_focus_to_trigger(operator_page: Page, flow: Flow) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/fleet")

    trigger, dialog = _propose(operator_page, flow)
    operator_page.keyboard.press("Escape")

    expect(dialog).to_be_hidden()
    expect(trigger).to_be_focused()


@FLOWS
def test_cancel_closes_dialog_and_returns_focus_to_trigger(operator_page: Page, flow: Flow) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/fleet")

    trigger, dialog = _propose(operator_page, flow)
    dialog.get_by_role("button", name="Cancel").click()

    expect(dialog).to_be_hidden()
    expect(trigger).to_be_focused()


@pytest.mark.skipif(bool(live_base_url()), reason="confirming would mutate a live stack")
@FLOWS
def test_confirm_renders_result_badge_from_fixture(operator_page: Page, flow: Flow) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/fleet")

    _trigger, dialog = _propose(operator_page, flow)
    dialog.get_by_role("button", name=flow.confirm).click()

    expect(dialog).to_be_hidden()
    result = operator_page.locator(flow.confirm_result)
    expect(result).to_have_count(1)
    expect(result.locator(".status-badge .status-text")).to_have_text(flow.fixture_badge)
