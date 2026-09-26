"""K15 territory predicate and market membership (09 D1/D2, C25 a/b; TS-19-03/04/05/31)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from opengrid.core.models.engine import Contract
from opengrid.market.territory import (
    FREE,
    R_TERRITORY_NO_FREE_ACCESS,
    R_TERRITORY_OUTSIDE,
    R_TERRITORY_UNKNOWN,
    MarketModelError,
    MarketRef,
    check_territory,
    market_of,
    market_of_contract,
    territory_of_zone,
)

AE = MarketRef("REGULATED", "AUSTIN_ENERGY")
CPS = MarketRef("REGULATED", "CPS_ENERGY")
ZONES = {"LZ_AEN": "AUSTIN_ENERGY", "LZ_CPS": "CPS_ENERGY"}


def _contract(**kw: object) -> Contract:
    base: dict[str, object] = {
        "contract_id": uuid4(),
        "customer_id": uuid4(),
        "service_type": "ERCOT_ENERGY",
        "tier": "T2",
        "profile_ref": "p@1",
        "start_at": datetime(2026, 9, 1, tzinfo=UTC),
    }
    base.update(kw)
    return Contract.model_validate(base)


def test_existing_contract_rows_default_to_free() -> None:
    contract = _contract()
    assert contract.market == "FREE"
    assert contract.utility_id is None
    assert market_of_contract(contract) == FREE


def test_regulated_capacity_contract_resolves_to_its_utility() -> None:
    contract = _contract(service_type="REGULATED_CAPACITY", market="REGULATED", utility_id="AUSTIN_ENERGY")
    assert market_of_contract(contract) == AE


@pytest.mark.parametrize(
    ("market", "utility_id", "service_type"),
    [
        ("REGULATED", None, "REGULATED_CAPACITY"),
        ("FREE", "AUSTIN_ENERGY", "ERCOT_ENERGY"),
        ("FREE", None, "REGULATED_CAPACITY"),
        (None, None, "REGULATED_CAPACITY"),
        (None, "CPS_ENERGY", "ERCOT_ENERGY"),
        ("REGULATED", "SOME_OTHER_UTILITY", "REGULATED_CAPACITY"),
        ("SPOT", None, "ERCOT_ENERGY"),
    ],
)
def test_inconsistent_market_rows_fail_closed(
    market: str | None, utility_id: str | None, service_type: str
) -> None:
    with pytest.raises(MarketModelError):
        market_of(market=market, utility_id=utility_id, service_type=service_type)


def test_null_market_is_free() -> None:
    assert market_of(market=None, utility_id=None, service_type="ERCOT_AS") == FREE


def test_market_ref_rejects_inconsistent_construction() -> None:
    with pytest.raises(MarketModelError):
        MarketRef("REGULATED")
    with pytest.raises(MarketModelError):
        MarketRef("FREE", "AUSTIN_ENERGY")


def test_territory_of_zone() -> None:
    assert territory_of_zone("LZ_AEN", ZONES) == "AUSTIN_ENERGY"
    assert territory_of_zone("LZ_CPS", ZONES) == "CPS_ENERGY"
    assert territory_of_zone("LZ_NORTH", ZONES) == "ERCOT_COMPETITIVE"
    assert territory_of_zone("HB_HUBAVG", ZONES) is None
    assert territory_of_zone("", ZONES) is None
    assert territory_of_zone(None, ZONES) is None


def test_ae_obligation_rejects_oncor_bank() -> None:
    """TS-19-03/31: an AE REG obligation served by an Oncor-area (competitive) asset is refused."""
    assert check_territory(AE, "ERCOT_COMPETITIVE") == R_TERRITORY_OUTSIDE
    assert check_territory(AE, "CPS_ENERGY") == R_TERRITORY_OUTSIDE
    assert check_territory(AE, "AUSTIN_ENERGY") is None


def test_territory_asset_on_free_needs_access() -> None:
    """TS-19-05/31: an AE asset on FREE headroom with access off is refused; access on allows it."""
    assert check_territory(FREE, "AUSTIN_ENERGY") == R_TERRITORY_NO_FREE_ACCESS
    assert check_territory(FREE, "AUSTIN_ENERGY", free_access=True) is None
    assert check_territory(FREE, "ERCOT_COMPETITIVE") is None


def test_unknown_territory_or_market_fails_closed() -> None:
    assert check_territory(AE, None) == R_TERRITORY_UNKNOWN
    assert check_territory(None, "AUSTIN_ENERGY") == R_TERRITORY_UNKNOWN
    assert check_territory(FREE, None, free_access=True) == R_TERRITORY_UNKNOWN
    # A raw DB string that is not a known territory never passes, even with access on.
    assert check_territory(FREE, "LZ_NORTH", free_access=True) == R_TERRITORY_UNKNOWN
    assert check_territory(AE, "austin_energy") == R_TERRITORY_UNKNOWN


_TERRITORIES = st.sampled_from(["AUSTIN_ENERGY", "CPS_ENERGY", "ERCOT_COMPETITIVE", "LZ_BOGUS", "", None])
_REFS = st.sampled_from([AE, CPS, FREE, None])


@given(ref=_REFS, territory=_TERRITORIES, access=st.booleans())
def test_property_regulated_obligations_never_leave_their_territory(
    ref: MarketRef | None, territory: str | None, access: bool
) -> None:
    """TS-19-04 (property): PASS for a REG(u) obligation implies the asset is inside u; PASS for FREE
    implies a competitive asset or a territory with access; unknown never passes."""
    verdict = check_territory(ref, territory, free_access=access)  # type: ignore[arg-type]
    if verdict is None:
        assert ref is not None and territory is not None
        if ref.is_regulated:
            assert territory == ref.utility_id
        else:
            assert territory == "ERCOT_COMPETITIVE" or (
                access and territory in ("AUSTIN_ENERGY", "CPS_ENERGY")
            )


# --- NOIE zones (issue #36 point 4; not yet labelled in tdsp_tariffs.toml, owner decision pending) -----


def test_noie_serves_neither_market() -> None:
    assert check_territory(AE, "NOIE") == R_TERRITORY_OUTSIDE
    assert check_territory(CPS, "NOIE") == R_TERRITORY_OUTSIDE
    assert check_territory(FREE, "NOIE") == R_TERRITORY_NO_FREE_ACCESS
    # The access flag is a regulated customer's contract term; it never opens a NOIE zone to ERCOT.
    assert check_territory(FREE, "NOIE", free_access=True) == R_TERRITORY_NO_FREE_ACCESS


def test_territory_of_zone_returns_noie() -> None:
    owners = {**ZONES, "LZ_LCRA": "NOIE"}
    assert territory_of_zone("LZ_LCRA", owners) == "NOIE"  # type: ignore[arg-type]
    assert territory_of_zone("LZ_LCRA", ZONES) == "ERCOT_COMPETITIVE"  # regulated-only map: unchanged
