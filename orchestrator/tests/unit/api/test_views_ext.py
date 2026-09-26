"""`opengrid.api.views_ext`: labels and assembly of the read-only settlement view (pure parts), plus auth."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from opengrid.api import views_ext
from opengrid.api.views_ext import (
    _period,
    build_view,
    contract_label,
    customer_names,
    obligation_label,
    short_code,
)
from opengrid.platform.config import Config

D03 = UUID("00000000-0000-7000-8000-000000000d03")
C03 = UUID("00000000-0000-7000-8000-000000000c03")
C05 = UUID("00000000-0000-7000-8000-000000000c05")
D05 = UUID("00000000-0000-7000-8000-000000000d05")
OBL = UUID("8e1cdcde-299a-4143-8234-f56c0ac6192c")


def test_short_code_and_contract_label() -> None:
    assert short_code(D03) == "d03"
    assert contract_label("ERCOT_AS", D03) == "ERCOT_AS · d03"


def test_customer_names_from_role_config() -> None:
    cfg = Config({"api": {"roles": {"customer": {"og-cust-ercot": str(C03)}}}})
    assert customer_names(cfg) == {str(C03): "og-cust-ercot"}
    assert customer_names(Config({})) == {}


def test_obligation_label_is_window_product_and_kw_in_ercot_time() -> None:
    start = datetime(2026, 9, 26, 17, 0, tzinfo=UTC)  # 12:00 CDT
    end = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
    assert obligation_label(start, end, "ECRS", Decimal("500.000")) == "Sep 26 12:00\u201313:00 ECRS 500 kW"
    overnight = obligation_label(
        datetime(2026, 9, 27, 4, 30, tzinfo=UTC), datetime(2026, 9, 27, 5, 30, tzinfo=UTC), None, None
    )
    assert overnight == "Sep 26 23:30\u2013Sep 27 00:30"


def test_period_defaults_and_rejects_bad_ranges() -> None:
    d0, d1 = _period(None, date(2026, 9, 26))
    assert d1 == date(2026, 9, 27) and (d1 - d0).days == 31
    with pytest.raises(HTTPException):
        _period(date(2026, 9, 27), date(2026, 9, 1))


def test_build_view_labels_both_screens_from_one_contract_join() -> None:
    view = build_view(
        contracts=[
            {
                "contract_id": D03,
                "customer_id": C03,
                "service_type": "ERCOT_AS",
                "variant": "ECRS",
                "tier": "T2",
            },
            {
                "contract_id": D05,
                "customer_id": C05,
                "service_type": "PARTNER_CAPACITY",
                "variant": "EVENT",
                "tier": "T3",
            },
        ],
        obligations=[
            {
                "obligation_id": OBL,
                "contract_id": D03,
                "service_type": "ERCOT_AS",
                "window_start": datetime(2026, 9, 26, 17, 0, tzinfo=UTC),
                "window_end": datetime(2026, 9, 26, 18, 0, tzinfo=UTC),
                "committed_qty_kw": Decimal("500.000"),
                "state": "SETTLED",
            }
        ],
        pnl_rows=[
            {"pnl_id": UUID(int=1), "obligation_id": OBL, "contract_id": D03, "net_value": Decimal("-1.5")}
        ],
        invoice_lines=[
            {
                "invoice_line_id": UUID(int=2),
                "obligation_id": OBL,
                "contract_id": D03,
                "period_start": date(2026, 9, 26),
            }
        ],
        last_updated={
            "pnl": datetime(2026, 9, 26, 17, 45, tzinfo=UTC),
            "invoice_line": None,
            "meter_interval": None,
        },
        names={str(C03): "og-cust-ercot"},
        period=(date(2026, 8, 27), date(2026, 9, 27)),
    )
    labels = {c["contract_id"]: (c["label"], c["customer_label"]) for c in view["contracts"]}
    assert labels[str(D03)] == ("ERCOT_AS · d03", "og-cust-ercot")
    assert labels[str(D05)] == ("PARTNER_CAPACITY · d05", "c05")  # unmapped customer: its short code
    assert view["obligations"][str(OBL)]["label"] == "Sep 26 12:00\u201313:00 ECRS 500 kW"
    assert view["pnl_rows"][0] == {
        "pnl_id": str(UUID(int=1)),
        "obligation_id": str(OBL),
        "contract_id": str(D03),
        "net_value": "-1.5",
    }
    assert view["invoice_lines"][0]["period_start"] == "2026-09-26"
    assert view["last_updated"]["pnl"] == "2026-09-26T17:45:00+00:00"


def test_settlement_view_requires_a_proxy_asserted_viewer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OG_API_PROXY_SECRET", "s3cret")
    app = FastAPI()
    app.include_router(views_ext.router)
    app.state.config = Config({"api": {"roles": {"viewer": ["viewer"]}}})
    client = TestClient(app)
    assert client.get("/og/api/views/settlement").status_code == 401
    assert client.get("/og/api/views/settlement", headers={"X-Remote-User": "viewer"}).status_code == 401
    spoof = {"X-Remote-User": "viewer", "X-OG-Proxy-Auth": "wrong"}
    assert client.get("/og/api/views/settlement", headers=spoof).status_code == 401


def test_create_app_mounts_the_settlement_view() -> None:
    from opengrid.api.app import create_app

    assert "/og/api/views/settlement" in create_app().openapi()["paths"]
