"""D-37 (supersedes D-32): LZ_LCRA / LZ_RAYBN are regulated (NOIE) territory with no contract.

Covers the repo config (territory, no M1), K15 on those banks, the availability representation and its
API fields, the sample-contract naming/flag (seed + migration), and the grandfather time rule."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from opengrid.core.models.engine import Contract
from opengrid.core.models.market import (
    AVAILABILITY_BADGE,
    AVAILABILITY_TEXT,
    SAMPLE_INACTIVE_LABEL,
    UTILITY_IDS,
)
from opengrid.market.availability import (
    BankAvailability,
    availability_fields,
    available_kw,
    contract_labels,
    grandfathered_banks_by_obligation,
    is_grandfathered,
    parse_availability,
    unavailable_bank_ids,
    with_availability,
)
from opengrid.market.config import DEFAULT_UTILITIES, load_zone_owners, load_zone_territory
from opengrid.market.model import MarketModel, load_market_model
from opengrid.market.territory import FREE, R_TERRITORY_NO_FREE_ACCESS, MarketRef, check_territory
from opengrid.settle.tariffs import load_tdsp_tariffs, tdsp_for_zone

REPO = Path(__file__).resolve().parents[4]
TARIFFS = REPO / "orchestrator" / "config" / "tdsp_tariffs.toml"
DEV_TARIFFS = REPO / "dev" / "config" / "tdsp_tariffs.toml"
SEED = REPO / "dev" / "seed" / "noie_switch_seed.sql"
MARKET_SEED = REPO / "dev" / "seed" / "market_model_seed.sql"
MIGRATION = REPO / "orchestrator" / "migrations" / "0046_noie_switch.sql"

BADGE = "Regulated market – no contract"  # noqa: RUF001 -- the owner's wording
TEXT = (
    "Unavailable: regulated (NOIE) territory, so energy can't be sold into ERCOT, and there is no utility "
    "capacity contract to reserve it. Available once a contract is signed."
)


# --- territory ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", [TARIFFS, DEV_TARIFFS])
def test_lcra_and_raybn_are_regulated_territory_in_both_tariff_files(path: Path) -> None:
    territory = load_zone_territory(path)
    assert territory["LZ_LCRA"] == "LCRA"
    assert territory["LZ_RAYBN"] == "RAYBURN"
    assert load_zone_owners(path)["LZ_LCRA"] == "LCRA"  # REGULATED, not NOIE (NOIE serves neither market)


def test_lcra_and_rayburn_are_known_utilities_with_placeholder_terms() -> None:
    assert {"LCRA", "RAYBURN"} <= set(UTILITY_IDS)
    for uid in ("LCRA", "RAYBURN"):
        u = DEFAULT_UTILITIES[uid]
        assert u.capacity_price_usd_per_kw == Decimal("102")
        assert u.capacity_product == "UTILITY_TOLLING"
        assert "PLACEHOLDER" in (u.source_note or "")
    assert DEFAULT_UTILITIES["LCRA"].name == "LCRA (Lower Colorado River Authority)"
    assert DEFAULT_UTILITIES["RAYBURN"].name == "Rayburn Country Electric Cooperative"


@pytest.mark.parametrize("path", [TARIFFS, DEV_TARIFFS])
def test_no_m1_delivery_charge_in_lcra_or_raybn(path: Path) -> None:
    _tariffs, zone_default_tdsp = load_tdsp_tariffs(path)
    assert tdsp_for_zone(zone_default_tdsp, "LZ_LCRA") is None
    assert tdsp_for_zone(zone_default_tdsp, "LZ_RAYBN") is None
    model = load_market_model(banks=[("bank-050", "LZ_LCRA"), ("bank-060", "LZ_RAYBN")], config_path=path)
    cost = model.charging_cost("LZ_LCRA", datetime(2026, 9, 28, 8, 0, tzinfo=UTC))
    assert cost.delivery_usd_per_kwh == 0


def test_k15_blocks_ercot_offers_on_lcra_and_raybn_banks() -> None:
    model = load_market_model(
        banks=[("bank-050", "LZ_LCRA"), ("bank-060", "LZ_RAYBN"), ("bank-000", "LZ_NORTH")],
        config_path=TARIFFS,
    )
    assert model.eligible_bank_ids(FREE) == frozenset({"bank-000"})
    assert model.eligible_bank_ids(MarketRef("REGULATED", "LCRA")) == frozenset({"bank-050"})
    assert model.eligible_bank_ids(MarketRef("REGULATED", "RAYBURN")) == frozenset({"bank-060"})
    assert check_territory(FREE, "LCRA") == R_TERRITORY_NO_FREE_ACCESS


def test_market_model_constructs_with_the_new_owners() -> None:
    model = MarketModel(
        zone_territory={"LZ_LCRA": "LCRA"}, utilities=DEFAULT_UTILITIES, banks=[("b", "LZ_LCRA")]
    )
    assert model.territory_of_bank("b") == "LCRA"


# --- availability ---------------------------------------------------------------------------------------


def test_owner_wording_is_defined_once() -> None:
    assert AVAILABILITY_BADGE["REGULATED_NO_CONTRACT"] == BADGE
    assert AVAILABILITY_TEXT["REGULATED_NO_CONTRACT"] == TEXT


def test_availability_fields_for_an_unavailable_bank() -> None:
    assert availability_fields("UNAVAILABLE", "REGULATED_NO_CONTRACT") == {
        "availability": "UNAVAILABLE",
        "availability_reason": "REGULATED_NO_CONTRACT",
        "availability_text": TEXT,
        "availability_badge": BADGE,
    }


def test_rows_without_the_columns_read_available() -> None:
    row = with_availability({"hub_id": "hub-00001"})
    assert row["availability"] == "AVAILABLE"
    assert row["availability_reason"] is None and row["availability_text"] is None
    assert (
        with_availability(
            {"hub_id": "h", "availability": "UNAVAILABLE", "availability_reason": "REGULATED_NO_CONTRACT"}
        )["availability_badge"]
        == BADGE
    )


def test_an_unknown_availability_value_fails_closed() -> None:
    assert not parse_availability("b", "MAYBE", None, None).available


def test_unavailable_kw_is_reported_on_its_own_line() -> None:
    banks = [
        parse_availability("b1", "AVAILABLE", None, None),
        parse_availability("b2", "UNAVAILABLE", "REGULATED_NO_CONTRACT", datetime.now(UTC)),
    ]
    assert available_kw({"b1": 600.0, "b2": 600.0}, unavailable_bank_ids(banks)) == (600.0, 600.0)


def test_grandfathering_needs_an_obligation_older_than_the_switch() -> None:
    switch = datetime(2026, 9, 27, 4, 0, tzinfo=UTC)
    bank = BankAvailability("bank-067", "UNAVAILABLE", "REGULATED_NO_CONTRACT", switch)
    assert is_grandfathered(switch - timedelta(hours=1), bank)
    assert not is_grandfathered(switch + timedelta(seconds=1), bank)  # a NEW commitment: never
    assert not is_grandfathered(None, bank)
    assert not is_grandfathered(switch - timedelta(hours=1), BankAvailability("bank-000"))
    assert grandfathered_banks_by_obligation([("o1", "bank-067"), ("o1", "bank-066")]) == {
        "o1": frozenset({"bank-066", "bank-067"})
    }


# --- sample contracts -----------------------------------------------------------------------------------


def _sample(name: str, utility: str) -> Contract:
    return Contract(
        contract_id=uuid4(),
        customer_id=uuid4(),
        service_type="REGULATED_CAPACITY",
        variant="TOLLING",
        tier="T1",
        profile_ref="regulated-tolling-profile@1",
        start_at=datetime.now(UTC),
        status="SUSPENDED",
        market="REGULATED",
        utility_id=utility,
        name=name,
        is_sample=True,  # type: ignore[arg-type]
    )


def test_sample_contract_is_labelled_sample_inactive_with_the_no_contract_reason() -> None:
    out = contract_labels(_sample("Sample Contract: LCRA Tolling (placeholder terms)", "LCRA"))
    assert out["label"] == SAMPLE_INACTIVE_LABEL == "SAMPLE – INACTIVE"  # noqa: RUF001
    assert out["is_sample"] is True and out["status"] == "SUSPENDED"
    assert out["availability_reason"] == "REGULATED_NO_CONTRACT"


def test_seed_names_flags_and_suspends_both_samples() -> None:
    sql = SEED.read_text(encoding="utf-8")
    names = re.findall(r"'(Sample Contract: [^']+)'", sql)
    assert names == [
        "Sample Contract: LCRA Tolling (placeholder terms)",
        "Sample Contract: Rayburn Tolling (placeholder terms)",
    ]
    assert sql.count("'SUSPENDED', 'REGULATED'") == 2
    assert "'TOLLING'" in sql and "6000, 0, true, 90, 'BINARY'" in sql
    assert "'REGULATED_NO_CONTRACT'" in sql
    # Only LCRA/RAYBURN: no other utility, zone or contract id is written.
    for other in ("AUSTIN_ENERGY", "CPS_ENERGY", "LZ_AEN", "LZ_NORTH", "ae0d"):
        assert other not in sql
    # Never an obligation/reservation/commitment/grant write.
    for table in ("og.obligation", "og.reservation", "og.commitment", "og.grant", "DELETE"):
        assert table not in sql


def test_market_seed_carries_both_utilities() -> None:
    sql = MARKET_SEED.read_text(encoding="utf-8")
    assert "('LCRA', 'LCRA (Lower Colorado River Authority)'" in sql
    assert "('RAYBURN', 'Rayburn Country Electric Cooperative'" in sql


def test_migration_forbids_an_active_or_misnamed_sample() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "CHECK (NOT is_sample OR status <> 'ACTIVE')" in sql
    assert "name LIKE 'Sample Contract%'" in sql
    assert "'LCRA', 'RAYBURN'" in sql
    assert "availability_reason IN ('REGULATED_NO_CONTRACT')" in sql
