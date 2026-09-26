"""Accessibility basics on every screen (BUILD.md UI brief, UI-UX spec S5): labelled form controls, a
single `<h1>`, header cells on every table, `lang` on `<html>`, unique ids, a modal `role` on every
dialog, and WCAG AA contrast computed from the stylesheet's own colour tokens for both themes."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from contrast import AA_BODY_TEXT, AA_LARGE_OR_UI, contrast_ratio, mix_srgb, parse_theme_tokens
from screens import BASE_PATH, SCREENS, goto_ok

_STYLESHEET_PATH = f"{BASE_PATH}/static/og.css"
_SAFESTOP_FILL_MIX = 0.12  # `.btn-safestop` background: color-mix(in srgb, var(--status-critical) 12%, panel)

_UNLABELLED_CONTROLS = """
() => Array.from(document.querySelectorAll("input:not([type=hidden]), select, textarea"))
  .filter((el) => el.labels.length === 0 && !el.getAttribute("aria-label") && !el.getAttribute("aria-labelledby"))
  .map((el) => el.outerHTML)
"""
_DUPLICATE_IDS = """
() => {
  const seen = new Map();
  document.querySelectorAll("[id]").forEach((el) => seen.set(el.id, (seen.get(el.id) || 0) + 1));
  return Array.from(seen).filter(([, n]) => n > 1).map(([id]) => id);
}
"""

# (foreground token, background token, minimum ratio) -- every text-on-surface pairing og.css produces.
# Body text (<= 13px everywhere in this UI, so nothing qualifies as WCAG "large") needs 4.5; the focus
# ring is a non-text UI indicator (WCAG 1.4.11) and needs 3.0.
_SURFACES = ("bg", "panel", "panel-2")
_TEXT_TOKENS = (
    "text",
    "muted",
    "kicker",
    "status-good",
    "status-info",
    "status-caution",
    "status-critical",
    "status-neutral",
)
_TEXT_PAIRS = tuple((fg, bg, AA_BODY_TEXT) for fg in _TEXT_TOKENS for bg in _SURFACES)
_UI_PAIRS = tuple(("focus-ring", bg, AA_LARGE_OR_UI) for bg in _SURFACES)


@pytest.mark.parametrize(("path", "_h1"), SCREENS)
def test_page_structure_basics(operator_page: Page, path: str, _h1: str) -> None:
    goto_ok(operator_page, path)

    expect(operator_page.locator("html")).to_have_attribute("lang", "en")
    expect(operator_page.locator("h1")).to_have_count(1)
    expect(operator_page.locator("table:not(:has(th))")).to_have_count(0)
    expect(operator_page.locator("main")).to_have_count(1)
    expect(operator_page.locator("nav[aria-label]")).to_have_count(1)


@pytest.mark.parametrize(("path", "_h1"), SCREENS)
def test_every_form_control_has_a_label(operator_page: Page, path: str, _h1: str) -> None:
    goto_ok(operator_page, path)

    assert operator_page.evaluate(_UNLABELLED_CONTROLS) == []


@pytest.mark.parametrize(("path", "_h1"), SCREENS)
def test_ids_are_unique(operator_page: Page, path: str, _h1: str) -> None:
    goto_ok(operator_page, path)

    assert operator_page.evaluate(_DUPLICATE_IDS) == []


def test_confirm_dialog_is_a_labelled_modal(operator_page: Page) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/fleet")
    form = operator_page.locator("#manual-command-form")
    form.get_by_label("Bank id", exact=True).fill("bank-01")
    form.get_by_label("Setpoint (kW)", exact=True).fill("5.0")
    form.get_by_label("Reason", exact=True).fill("a11y check")
    form.get_by_role("button", name="Propose (step 1 of 2)").click()

    dialog = operator_page.locator(".confirm-dialog")
    expect(dialog).to_be_visible()
    # an alertdialog is the confirm-flavoured subclass of dialog; either is a modal dialog role
    assert dialog.get_attribute("role") in ("dialog", "alertdialog")
    expect(dialog).to_have_attribute("aria-modal", "true")
    labelled_by = dialog.get_attribute("aria-labelledby")
    assert labelled_by
    expect(operator_page.locator(f"#{labelled_by}")).to_have_text("Confirm manual command")
    assert operator_page.evaluate(_DUPLICATE_IDS) == []


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_wcag_aa_contrast_of_theme_tokens(viewer_page: Page, theme: str) -> None:
    css = viewer_page.request.get(_STYLESHEET_PATH)
    assert css.status == 200
    tokens = parse_theme_tokens(css.text(), theme)

    failures = [
        f"{theme}: --{fg} on --{bg} = {contrast_ratio(tokens[fg], tokens[bg]):.2f} < {minimum}"
        for fg, bg, minimum in (*_TEXT_PAIRS, *_UI_PAIRS)
        if contrast_ratio(tokens[fg], tokens[bg]) < minimum
    ]
    safestop_fill = mix_srgb(tokens["status-critical"], tokens["panel"], _SAFESTOP_FILL_MIX)
    if (ratio := contrast_ratio(tokens["status-critical"], safestop_fill)) < AA_BODY_TEXT:
        failures.append(f"{theme}: --status-critical on .btn-safestop fill = {ratio:.2f} < {AA_BODY_TEXT}")
    assert failures == []
