"""D-37 operator API: the Dispatch refusal on an unavailable (regulated, no contract) hub carries the owner's
reason; a 0 kW hold is not refused (safe stop and maintenance keep working)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from opengrid.api.routers.fleet import _refuse_if_unavailable

LCRA_HUB = {
    "hub_id": "hub-02500",
    "availability": "UNAVAILABLE",
    "availability_reason": "REGULATED_NO_CONTRACT",
}


def test_a_manual_discharge_on_an_lcra_hub_is_refused_with_the_reason() -> None:
    with pytest.raises(HTTPException) as err:
        _refuse_if_unavailable([LCRA_HUB], -5.0)
    assert err.value.status_code == 409
    detail = err.value.detail
    assert detail["reason_code"] == "R-BANK-UNAVAILABLE-REGULATED-NO-CONTRACT"
    assert detail["availability_badge"] == "Regulated market – no contract"  # noqa: RUF001
    assert "Available once a contract is signed." in detail["message"]


def test_a_zero_kw_hold_and_an_available_hub_are_not_refused() -> None:
    _refuse_if_unavailable([LCRA_HUB], 0.0)
    _refuse_if_unavailable([{"hub_id": "hub-00001"}], -5.0)


def test_settlement_view_labels_a_sample_contract_sample_inactive() -> None:
    from datetime import date

    from opengrid.api.views_settlement import build_view

    view = build_view(
        contracts=[
            {
                "contract_id": "00000000-0000-7000-8000-00000000ac1d",
                "customer_id": "00000000-0000-7000-8000-00000000ac1c",
                "service_type": "REGULATED_CAPACITY",
                "variant": "TOLLING",
                "tier": "T1",
                "status": "SUSPENDED",
                "name": "Sample Contract: LCRA Tolling (placeholder terms)",
                "is_sample": True,
            },
            {
                "contract_id": "00000000-0000-7000-8000-00000000ae0d",
                "customer_id": "00000000-0000-7000-8000-00000000ae0c",
                "service_type": "REGULATED_CAPACITY",
                "variant": "TOLLING",
                "tier": "T1",
                "status": "ACTIVE",
            },
        ],
        obligations=[],
        pnl_rows=[],
        invoice_lines=[],
        last_updated={},
        names={},
        period=(date(2026, 9, 1), date(2026, 9, 27)),
    )
    sample, real = view["contracts"]
    assert (
        sample["label"] == "SAMPLE \u2013 INACTIVE \u00b7 Sample Contract: LCRA Tolling (placeholder terms)"
    )
    assert sample["is_sample"] and sample["sample_label"] == "SAMPLE \u2013 INACTIVE"
    assert real["label"] == "REGULATED_CAPACITY \u00b7 e0d" and not real["is_sample"]


def test_capacity_summary_keeps_unavailable_kw_out_of_available_kw() -> None:
    from opengrid.market.availability import capacity_summary

    out = capacity_summary(
        [
            {"bank_id": "bank-000", "zone": "LZ_NORTH", "kva_rating": 600},
            {
                "bank_id": "bank-050",
                "zone": "LZ_LCRA",
                "kva_rating": 600,
                "availability": "UNAVAILABLE",
                "availability_reason": "REGULATED_NO_CONTRACT",
            },
        ]
    )
    assert (out["available_kw"], out["unavailable_kw"]) == (600.0, 600.0)
    assert out["unavailable_badge"] == "Regulated market \u2013 no contract"
    zones = {z["zone"]: z for z in out["zones"]}
    assert zones["LZ_LCRA"]["availability"] == "UNAVAILABLE"
    assert zones["LZ_NORTH"]["availability"] == "AVAILABLE"
