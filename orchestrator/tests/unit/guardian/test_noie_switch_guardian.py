"""D-37 in the guardian (its own reads): G-33 vetoes a manual discharge -- and any non-idle item -- on an
UNAVAILABLE LCRA/RAYBN bank (R-BANK-UNAVAILABLE-REGULATED-NO-CONTRACT); a 0 kW hold passes (safe stop and
maintenance keep working); a K13-grandfathered obligation is signed untouched."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from opengrid.core.reasons import R_BANK_UNAVAILABLE
from opengrid.guardian import flow_checks
from opengrid.guardian.config import GuardianConfig
from opengrid.guardian.ports import ObligationMarket, ProposedItem
from opengrid.market.availability import BankAvailability

from .conftest import BANK_ID, make_batch_row
from .test_service_flow import FakeTerritory, _batch, _service

UNAVAILABLE = BankAvailability(
    BANK_ID, "UNAVAILABLE", "REGULATED_NO_CONTRACT", datetime(2026, 9, 27, tzinfo=UTC)
)


class LcraTerritory(FakeTerritory):
    def __init__(self) -> None:
        super().__init__()
        self.zones = {"hub-0001": "LZ_LCRA"}
        self.availability = {BANK_ID: UNAVAILABLE}

    def zone_territory(self):
        return {"LZ_AEN": "AUSTIN_ENERGY", "LZ_CPS": "CPS_ENERGY", "LZ_LCRA": "LCRA", "LZ_RAYBN": "RAYBURN"}


def _config() -> GuardianConfig:
    return GuardianConfig(key_path="", cycle_interval_s=2.0, default_feeder_ramp_ceiling_kw_per_min=1e9)


async def _verdict(fakes, seed, territory, items):
    proposal = _batch(fakes, items)
    return await _service(fakes, _config(), seed, territory=territory).evaluate_and_sign(
        make_batch_row(proposal)
    )


async def test_g33_vetoes_a_manual_discharge_on_an_lcra_hub(fakes, signing_seed):
    verdict = await _verdict(
        fakes, signing_seed, LcraTerritory(), [ProposedItem("hub-0001", -2.0, "R-MANUAL-RAMP")]
    )
    assert "G-33" in verdict.vetoed_rule_ids


async def test_g33_vetoes_a_manual_charge_on_an_unavailable_bank_too(fakes, signing_seed):
    """Idle hold: unlike an available regulated territory (D-29c), no manual charge either."""
    verdict = await _verdict(
        fakes, signing_seed, LcraTerritory(), [ProposedItem("hub-0001", 2.0, "R-MANUAL-RAMP")]
    )
    assert "G-33" in verdict.vetoed_rule_ids


async def test_a_zero_kw_hold_passes_so_safe_stop_still_works(fakes, signing_seed):
    verdict = await _verdict(
        fakes, signing_seed, LcraTerritory(), [ProposedItem("hub-0001", 0.0, "R-MANUAL-RAMP")]
    )
    assert "G-33" not in verdict.vetoed_rule_ids


async def test_a_new_ercot_obligation_is_vetoed_on_lcra(fakes, signing_seed):
    territory = LcraTerritory()
    oid = uuid4()
    territory.markets[oid] = ObligationMarket("FREE", None, "ERCOT_AS")
    verdict = await _verdict(
        fakes, signing_seed, territory, [ProposedItem("hub-0001", -3.0, "R-GRANT-COMMITTED", oid)]
    )
    assert "G-33" in verdict.vetoed_rule_ids


async def test_a_grandfathered_ercot_obligation_is_signed(fakes, signing_seed):
    territory = LcraTerritory()
    oid = uuid4()
    territory.markets[oid] = ObligationMarket("FREE", None, "ERCOT_AS")
    territory.grandfathered_pairs = {(str(oid), BANK_ID)}
    verdict = await _verdict(
        fakes, signing_seed, territory, [ProposedItem("hub-0001", -3.0, "R-GRANT-COMMITTED", oid)]
    )
    assert "G-33" not in verdict.vetoed_rule_ids


def test_check_g33_available_reason_code() -> None:
    item = ProposedItem("hub-0001", -1.0, "R-MANUAL-RAMP")
    outcome = flow_checks.check_g33_available(item, UNAVAILABLE)
    assert not outcome.ok and outcome.reason == R_BANK_UNAVAILABLE
    assert flow_checks.check_g33_available(item, BankAvailability(BANK_ID)).ok
    assert flow_checks.check_g33_available(item, None).ok  # unknown bank: G-34/K15 fail it closed
