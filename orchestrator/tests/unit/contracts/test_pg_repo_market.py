"""`pg_repo._contract_from_row` carries migration 0025's market columns (D-29 wiring)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from opengrid.contracts.pg_repo import _contract_from_row
from opengrid.market.territory import MarketRef, market_of_contract


def _row(**extra: object) -> dict[str, object]:
    row: dict[str, object] = {
        "contract_id": uuid4(),
        "customer_id": uuid4(),
        "service_type": "REGULATED_CAPACITY",
        "variant": "TOLLING",
        "tier": "T1",
        "profile_ref": "p@1",
        "territory_id": None,
        "start_at": datetime(2026, 9, 1, tzinfo=UTC),
        "end_at": None,
        "renomination_allowed": False,
        "penalty_alpha": None,
        "penalty_beta": None,
        "penalty_theta": None,
        "degradation_cost": Decimal("0.03"),
        "fallback_allowed": False,
        "status": "ACTIVE",
    }
    row.update(extra)
    return row


def test_regulated_row_keeps_its_market_and_utility() -> None:
    contract = _contract_from_row(_row(market="REGULATED", utility_id="AUSTIN_ENERGY"))
    assert (contract.market, contract.utility_id) == ("REGULATED", "AUSTIN_ENERGY")
    assert market_of_contract(contract) == MarketRef("REGULATED", "AUSTIN_ENERGY")


def test_row_without_the_columns_is_free() -> None:
    contract = _contract_from_row(_row(service_type="ERCOT_ENERGY", variant=None))
    assert (contract.market, contract.utility_id) == ("FREE", None)
    null_market = _contract_from_row(_row(service_type="ERCOT_ENERGY", market=None, utility_id=None))
    assert null_market.market == "FREE"
