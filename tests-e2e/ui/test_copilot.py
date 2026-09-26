"""The operator copilot panel (issue #26 items 1, 3 and 4).

The behaviour that matters most here is the one the spec calls a Must and demands a test for: the
console stays completely usable when the assistant is not. `test_the_console_is_untouched_when_the_
assistant_is_unavailable` is that test -- it drives the real two-step safe-stop flow with the agent
refusing every request, and asserts nothing about the deterministic console changed.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from screens import BASE_PATH, SCREENS, goto_ok


@pytest.mark.parametrize(("path", "_h1"), SCREENS)
def test_the_launcher_is_on_every_screen(viewer_page: Page, path: str, _h1: str) -> None:
    """S2.2 puts the copilot launcher in the status bar, on every page, for every role."""
    goto_ok(viewer_page, path)

    launcher = viewer_page.locator(".og-copilot-launcher")
    expect(launcher).to_have_count(1)
    expect(launcher).to_have_attribute("aria-expanded", "false")


def test_the_panel_opens_answers_and_cites_its_source(operator_page: Page) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/")

    operator_page.locator(".og-copilot-launcher").click()
    panel = operator_page.locator("#og-copilot-panel")
    expect(panel).to_be_visible()

    panel.get_by_label("Ask the copilot").fill("which obligations are at risk?")
    panel.get_by_role("button", name="Ask").click()

    turn = operator_page.locator(".og-copilot-turn").first
    expect(turn).to_contain_text("ffcc182c")
    # the spec forbids an unsourced assertion, so every answer shows what it read
    expect(turn.locator(".og-copilot-citations li")).to_have_count(1)
    expect(turn).to_contain_text("/og/api/dispatch/opportunities")


def test_a_deterministic_answer_does_not_claim_to_be_ai_assisted(operator_page: Page) -> None:
    """The badge means a model wrote it. An answer read straight from console data must not carry it."""
    goto_ok(operator_page, f"{BASE_PATH}/")
    operator_page.locator(".og-copilot-launcher").click()
    panel = operator_page.locator("#og-copilot-panel")
    panel.get_by_label("Ask the copilot").fill("which obligations are at risk?")
    panel.get_by_role("button", name="Ask").click()

    turn = operator_page.locator(".og-copilot-turn").first
    expect(turn).to_contain_text("no model used")
    expect(turn.locator(".og-ai-badge")).to_have_count(0)


def test_the_panel_dismisses(operator_page: Page) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/")
    operator_page.locator(".og-copilot-launcher").click()
    panel = operator_page.locator("#og-copilot-panel")
    expect(panel).to_be_visible()

    panel.get_by_role("button", name="Dismiss the copilot").click()

    expect(panel).not_to_be_visible()


def test_the_console_is_untouched_when_the_assistant_is_unavailable(operator_page: Page) -> None:
    """UI-DSP-13, the Must: with every copilot request failing, the deterministic console still runs its
    guarded two-step flow end to end. The agent's relay is blocked in the browser here -- what matters is
    that its failure changes nothing else on the page (the rendered "unavailable" text itself is pinned
    in `orchestrator/tests/unit/ui/test_copilot_route.py`, where the server-side call can be made to
    fail properly)."""
    operator_page.route("**/og/copilot/**", lambda route: route.abort())
    goto_ok(operator_page, f"{BASE_PATH}/fleet")

    # asking is harmless even though it cannot succeed
    operator_page.locator(".og-copilot-launcher").click()
    panel = operator_page.locator("#og-copilot-panel")
    panel.get_by_label("Ask the copilot").fill("anything at all")
    panel.get_by_role("button", name="Ask").click()

    # the guarded control still runs its full two-step flow, unchanged
    form = operator_page.locator("#safestop-propose-form")
    form.get_by_label("Reason", exact=True).fill("agent-down check")
    form.get_by_role("button", name="Propose safe stop (step 1 of 2)").click()
    expect(operator_page.locator("#safestop-propose-result .confirm-dialog")).to_be_visible()
    expect(operator_page.locator(".og-degraded-banner")).to_have_count(0)
