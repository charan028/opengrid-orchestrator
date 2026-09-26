"""Owner-reported defect (2026-09-26): Profitability and Billing & audit must name each obligation's contract
identically, show labels (not UUIDs) as visible text, and cross-link a P&L row to its invoice lines."""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from screens import BASE_PATH, goto_ok

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_CONTRACTS_JS = """(tableId) => {
  const out = {};
  document.querySelectorAll(`#${tableId} tbody tr[data-obligation-id]`).forEach((tr) => {
    const label = tr.querySelector('.contract-cell').innerText.trim();
    (out[tr.dataset.obligationId] = out[tr.dataset.obligationId] || []).push(label);
  });
  return out;
}"""


def _contracts(page: Page, table_id: str) -> dict[str, set[str]]:
    return {k: set(v) for k, v in page.evaluate(_CONTRACTS_JS, table_id).items()}


def test_contract_labels_match_across_profitability_and_billing(viewer_page: Page) -> None:
    goto_ok(viewer_page, f"{BASE_PATH}/profitability")
    on_pnl = _contracts(viewer_page, "pnl-table")
    assert not UUID_RE.search(viewer_page.locator("#pnl-table").inner_text())

    goto_ok(viewer_page, f"{BASE_PATH}/billing")
    on_billing = _contracts(viewer_page, "invoice-table")
    assert not UUID_RE.search(viewer_page.locator("#invoice-table").inner_text())

    common = set(on_pnl) & set(on_billing)
    assert common, "the fixture must share obligations across both screens"
    for obligation in common:
        assert on_pnl[obligation] == on_billing[obligation]
        assert len(on_pnl[obligation]) == 1


def test_profitability_row_links_to_its_invoice_lines(viewer_page: Page) -> None:
    goto_ok(viewer_page, f"{BASE_PATH}/profitability")
    row = viewer_page.locator("#pnl-table tbody tr[data-obligation-id]").first
    obligation = row.get_attribute("data-obligation-id")
    row.get_by_role("link", name="Invoice").click()
    expect(viewer_page).to_have_url(re.compile(rf"/og/billing\?obligation={obligation}"))
    expect(viewer_page.locator("#invoice-clear-obligation")).to_be_visible()
    ids = viewer_page.eval_on_selector_all(
        "#invoice-table tbody tr[data-obligation-id]", "rows => rows.map(r => r.dataset.obligationId)"
    )
    assert ids and set(ids) == {obligation}


def test_superseded_lines_are_struck_through(viewer_page: Page) -> None:
    goto_ok(viewer_page, f"{BASE_PATH}/billing")
    struck = viewer_page.locator("#invoice-table tr.is-superseded").first
    expect(struck).to_have_css("text-decoration-line", "line-through")
