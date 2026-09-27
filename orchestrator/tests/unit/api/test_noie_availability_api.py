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
