"""Profitability and Billing & audit read one settlement view (owner-reported defect 2026-09-26): human
labels instead of UUIDs, the same contract label on both screens, superseded rows struck and not counted,
filters by customer/contract, per-contract totals, and liveness timestamps."""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

import opengrid.ui.routes.billing_audit as billing_route
import opengrid.ui.routes.profitability as profitability_route
from opengrid.ui.settlement import SETTLEMENT_VIEW_PATH, filter_options, invoice_view, pnl_view

from .conftest import load_fixture

D03 = "00000000-0000-7000-8000-000000000d03"
D05 = "00000000-0000-7000-8000-000000000d05"
C05 = "00000000-0000-7000-8000-000000000c05"
OBL = "8e1cdcde-299a-4143-8234-f56c0ac6192c"
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")


@pytest.fixture
def view() -> dict[str, Any]:
    return load_fixture("views_settlement.json")


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch, view: dict[str, Any]) -> list[tuple[str, dict[str, Any] | None]]:
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def fake_get_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
        calls.append((path, params))
        if path == SETTLEMENT_VIEW_PATH:
            return view
        return {"lines": [], "performance": []} if "invoice-lines" in path else []

    monkeypatch.setattr(profitability_route, "get_json", fake_get_json)
    monkeypatch.setattr(billing_route, "get_json", fake_get_json)
    return calls


def test_pnl_view_excludes_superseded_rows_from_totals(view: dict[str, Any]) -> None:
    pnl = pnl_view(view)
    assert pnl["row_count"] == 3 and pnl["superseded_count"] == 1
    assert pnl["totals"]["net_value"] == pytest.approx(-25.903825 - 20.181050 + 10.0)
    by_contract = {g["label"]: round(g["total"], 6) for g in pnl["by_contract"]}
    assert by_contract == {"ERCOT_AS · d03": round(-25.903825 - 20.181050, 6), "PARTNER_CAPACITY · d05": 10.0}
    first = pnl["rows"][0]
    assert first["contract_label"] == "ERCOT_AS · d03" and first["customer_label"] == "og-cust-ercot"
    assert first["obligation_label"] == "Sep 26 12:00\u201313:00 ECRS 500 kW"
    assert first["interval_label"] == "Sep 26 12:15\u201312:30"
    assert first["invoice_link"] == f"/og/billing?obligation={OBL}#invoice-lines"


def test_invoice_view_strikes_the_superseded_line_and_counts_its_correction(view: dict[str, Any]) -> None:
    inv = invoice_view(view)
    assert inv["row_count"] == 3 and inv["superseded_count"] == 1
    assert inv["total_amount"] == pytest.approx(-24.571333 + 0.2 + 12.5)
    assert inv["totals_by_type"]["CAPACITY_PAYMENT"] == pytest.approx(12.7)
    correction = next(r for r in inv["rows"] if r["is_correction"])
    assert correction["amount"] == pytest.approx(0.2) and not correction["superseded"]


def test_filters_by_customer_contract_and_obligation(view: dict[str, Any]) -> None:
    assert {r["contract_id"] for r in pnl_view(view, customer=C05)["rows"]} == {D05}
    assert {r["contract_id"] for r in invoice_view(view, contract=D03)["rows"]} == {D03}
    assert {r["obligation_id"] for r in invoice_view(view, obligation=OBL)["rows"]} == {OBL}
    options = filter_options(view)
    assert [o["label"] for o in options["contracts"]] == ["ERCOT_AS · d03", "PARTNER_CAPACITY · d05"]
    assert {o["label"] for o in options["customers"]} == {"og-cust-ercot", "c05"}


def _contract_by_obligation(html: str, table_id: str) -> dict[str, set[str]]:
    table = html.split(f'id="{table_id}"')[1].split("</table>")[0]
    out: dict[str, set[str]] = {}
    for obligation, contract_label in re.findall(
        r'data-obligation-id="([^"]+)"[^>]*>\s*<td class="contract-cell"[^>]*>([^<]+)</td>', table
    ):
        out.setdefault(obligation, set()).add(contract_label)
    return out


def test_both_screens_name_each_obligations_contract_identically(
    client: TestClient, served: list[Any]
) -> None:
    headers = {"X-Remote-User": "viewer"}
    pnl_html = client.get("/og/profitability", headers=headers).text
    billing_html = client.get("/og/billing", headers=headers).text
    on_pnl = _contract_by_obligation(pnl_html, "pnl-table")
    on_billing = _contract_by_obligation(billing_html, "invoice-table")
    common = set(on_pnl) & set(on_billing)
    assert common == {OBL, "1ef1bee1-3e99-44ba-b3a6-99db12665bc2"}
    for obligation in common:
        assert on_pnl[obligation] == on_billing[obligation]
        assert len(on_pnl[obligation]) == 1


@pytest.mark.parametrize(
    ("path", "table_id"), [("/og/profitability", "pnl-table"), ("/og/billing", "invoice-table")]
)
def test_no_raw_uuid_is_visible_text(client: TestClient, served: list[Any], path: str, table_id: str) -> None:
    html = client.get(path, headers={"X-Remote-User": "viewer"}).text
    table = html.split(f'id="{table_id}"')[1].split("</table>")[0]
    visible = re.sub(r'\s(title|data-[a-z-]+|href)="[^"]*"', "", table)
    assert not UUID_RE.search(re.sub(r"<[^>]+>", " ", visible))
    assert "is-superseded" in table
    assert "Last" in html and "stale-badge" in html


def test_profitability_day_filter_asks_for_the_surrounding_utc_days(
    client: TestClient, served: list[Any]
) -> None:
    client.get("/og/profitability", params={"day": "2026-09-26"}, headers={"X-Remote-User": "viewer"})
    assert (SETTLEMENT_VIEW_PATH, {"from": "2026-09-25", "to": "2026-09-27"}) in served


def test_billing_obligation_cross_link_shows_only_that_obligation(
    client: TestClient, served: list[Any]
) -> None:
    html = client.get("/og/billing", params={"obligation": OBL}, headers={"X-Remote-User": "viewer"}).text
    assert set(_contract_by_obligation(html, "invoice-table")) == {OBL}
    assert 'id="invoice-clear-obligation"' in html
