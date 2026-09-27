"""G-19 corroboration on the guardian's own reads, matching the allocator's bounds (DISPATCH review request):
derating (F1: SoC, temperature, BMS limit), PQ eligibility (K14, G-24) and territory (K15, G-33). A shortfall
the allocator could not avoid for one of those causes is signed; the same claim on a bank that could have
delivered is vetoed."""

from __future__ import annotations

import dataclasses
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

from opengrid.guardian.ports import HubFlowTelemetry, ObligationMarket, ProposedItem, Reading
from opengrid.guardian.pq_ports import HubAssetSnapshot

from .conftest import (
    BANK_ID,
    FakeBankMembers,
    make_batch_row,
    make_hub_snapshot,
    make_proposal,
    service_with,
    wire_default_passing_scenario,
)
from .test_service_flow import FakeTerritory
from .test_service_pq import CLEAN, LIMITS, _Pq


def _reduced(fakes, reason_code: str, *, frozen_kw: str = "5.0", granted_kw: str = "2.0"):
    obligation_id = uuid4()
    proposal = make_proposal(
        obligation_id=obligation_id, obligation_granted_kw=Decimal(granted_kw), reason_code=reason_code
    )
    wire_default_passing_scenario(fakes, proposal)
    fakes.commitments.frozen[obligation_id] = Decimal(frozen_kw)
    fakes.prior_grants.prior[obligation_id] = Decimal(frozen_kw)
    return proposal, obligation_id


def _hub(*, temp_c: float | None = None, bms_kw: float | None = None, soc_kwh: float = 20.0):
    flow = HubFlowTelemetry(
        cell_temp_c=Reading(temp_c, 1.0) if temp_c is not None else None,
        p_dis_max_kw=Reading(bms_kw, 1.0) if bms_kw is not None else None,
    )
    return replace(make_hub_snapshot(soc_kwh=soc_kwh, p_kw=5.0), flow=flow)


def _members(fakes, hubs: dict) -> None:
    fakes.bank_members = FakeBankMembers()
    fakes.bank_members.members[BANK_ID] = list(hubs.values())
    fakes.bank_members.hub_ids[BANK_ID] = list(hubs)
    fakes.hubs.hubs.update(hubs)


def _reasons(fakes) -> set:
    return {v["reason"] for v in fakes.trace.appended[-1][1]["violations"]}


# --- derating ---------------------------------------------------------------------------------------------


async def test_a_temperature_derating_shortfall_is_corroborated(fakes, guardian_config, signing_seed):
    """2 x 5 kW hubs at 50 C: F1's discharge temperature factor is 0.4, so the bank can deliver 4 kW < the
    5 kW commitment. Before the fix the guardian counted the raw 10 kW rating and vetoed the claim."""
    proposal, _ = _reduced(fakes, "R-COMMIT-LOCK-INFEASIBLE")
    _members(fakes, {"hub-a": _hub(temp_c=50.0), "hub-b": _hub(temp_c=50.0)})

    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )

    assert verdict.outcome == "PASS"


async def test_a_bms_limit_shortfall_is_corroborated(fakes, guardian_config, signing_seed):
    proposal, _ = _reduced(fakes, "R-COMMIT-LOCK-INFEASIBLE")
    _members(fakes, {"hub-a": _hub(temp_c=25.0, bms_kw=1.0), "hub-b": _hub(temp_c=25.0, bms_kw=1.0)})

    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )

    assert verdict.outcome == "PASS"


async def test_an_underated_bank_that_could_deliver_still_vetoes_the_claim(
    fakes, guardian_config, signing_seed
):
    proposal, _ = _reduced(fakes, "R-COMMIT-LOCK-INFEASIBLE")
    _members(fakes, {"hub-a": _hub(temp_c=25.0), "hub-b": _hub(temp_c=25.0)})  # 10 kW available

    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )

    assert verdict.outcome == "VETOED" and "COMMIT_LOCK_OVERRIDE_INFEASIBLE_UNVERIFIED" in _reasons(fakes)


async def test_an_unknown_temperature_takes_the_allocators_factor(fakes, guardian_config, signing_seed):
    """No temperature ever reported: the allocator derates by 0.5, so 2 x 5 kW hubs offer 5 kW -- a 6 kW
    commitment is then infeasible and the claim is corroborated (it was vetoed at the raw 10 kW)."""
    proposal, _ = _reduced(fakes, "R-COMMIT-LOCK-INFEASIBLE", frozen_kw="6.0")
    _members(fakes, {"hub-a": _hub(), "hub-b": _hub()})

    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(
        make_batch_row(proposal)
    )

    assert verdict.outcome == "PASS"


# --- PQ eligibility (K14) -----------------------------------------------------------------------------------


@dataclasses.dataclass
class _PerHubPq(_Pq):
    assets: dict = dataclasses.field(default_factory=dict)

    async def snapshot(self, hub_id):
        return self.assets.get(hub_id, HubAssetSnapshot("OK", "CATEGORY_III"))


class _ServiceTypes:
    def __init__(self, types: dict) -> None:
        self.types = types

    async def service_type(self, obligation_id):
        return self.types.get(obligation_id)

    async def deployment_active(self, obligation_id):
        return False

    async def deployment_requested_kw(self, obligation_id):
        return None


def _pq_world(fakes, config, seed, service_type: str):
    """A PQ-sensitive bank: an 8 kW commitment granted 2 kW; members hub-a (OK) and hub-b (QUARANTINED, not
    PQ-eligible), 5 kW each at 25 C. The bank as a whole could deliver 10 kW; PQ-eligible hubs only 5 kW."""
    proposal, obligation_id = _reduced(fakes, "R-COMMIT-LOCK-INFEASIBLE", frozen_kw="8.0")
    _members(fakes, {"hub-a": _hub(temp_c=25.0), "hub-b": _hub(temp_c=25.0)})
    pq = _PerHubPq(limits=LIMITS, measurement=CLEAN)
    pq.assets["hub-b"] = HubAssetSnapshot("QUARANTINED", "CATEGORY_III")
    service = service_with(fakes, config, seed)
    service.ports = replace(
        fakes.as_ports(), pq=pq.ports(), as_awards=_ServiceTypes({obligation_id: service_type})
    )
    return service, proposal


async def test_a_pq_eligibility_shortfall_of_a_sensitive_obligation_is_corroborated(
    fakes, guardian_config, signing_seed
):
    service, proposal = _pq_world(fakes, guardian_config, signing_seed, "DATA_CENTER")

    verdict = await service.evaluate_and_sign(make_batch_row(proposal))

    assert "G-19" not in verdict.vetoed_rule_ids


async def test_pq_evidence_never_excuses_a_non_sensitive_obligation(fakes, guardian_config, signing_seed):
    service, proposal = _pq_world(fakes, guardian_config, signing_seed, "ERCOT_ENERGY")

    verdict = await service.evaluate_and_sign(make_batch_row(proposal))

    assert "G-19" in verdict.vetoed_rule_ids


async def test_no_pq_envelope_on_the_bank_means_no_pq_evidence(fakes, guardian_config, signing_seed):
    service, proposal = _pq_world(fakes, guardian_config, signing_seed, "DATA_CENTER")
    pq = _PerHubPq(limits=None, measurement=CLEAN)
    pq.assets["hub-b"] = HubAssetSnapshot("QUARANTINED", "CATEGORY_III")
    service.ports = replace(service.ports, pq=pq.ports())

    verdict = await service.evaluate_and_sign(make_batch_row(proposal))

    assert "G-19" in verdict.vetoed_rule_ids


# --- territory (K15) ----------------------------------------------------------------------------------------


def _territory_world(fakes, config, seed, market: ObligationMarket, reason: str = "R-TERRITORY-OUTSIDE"):
    obligation_id = uuid4()
    proposal = replace(
        make_proposal(), items=[ProposedItem("hub-0001", 0.0, reason, obligation_id, Decimal("0"))]
    )
    wire_default_passing_scenario(fakes, proposal)
    fakes.commitments.frozen[obligation_id] = Decimal("5.0")
    fakes.prior_grants.prior[obligation_id] = Decimal("5.0")
    territory = FakeTerritory()
    territory.markets[obligation_id] = market
    territory.zones = {"hub-0001": "LZ_NORTH"}
    service = service_with(fakes, config, seed)
    service.ports = replace(fakes.as_ports(), territory=territory)
    return service, proposal


async def test_a_territory_blocked_reduction_is_signed_when_the_guardian_agrees(
    fakes, guardian_config, signing_seed
):
    """An Austin Energy (REGULATED) obligation on an LZ_NORTH bank: the guardian's own K15 check blocks it,
    so the 0 kW grant carrying R-TERRITORY-OUTSIDE passes G-19."""
    service, proposal = _territory_world(
        fakes,
        guardian_config,
        signing_seed,
        ObligationMarket("REGULATED", "AUSTIN_ENERGY", "REGULATED_CAPACITY"),
    )

    verdict = await service.evaluate_and_sign(make_batch_row(proposal))

    assert "G-19" not in verdict.vetoed_rule_ids


async def test_a_territory_claim_on_an_eligible_bank_is_vetoed(fakes, guardian_config, signing_seed):
    service, proposal = _territory_world(
        fakes, guardian_config, signing_seed, ObligationMarket("FREE", None, "ERCOT_ENERGY")
    )

    verdict = await service.evaluate_and_sign(make_batch_row(proposal))

    assert "G-19" in verdict.vetoed_rule_ids and "TERRITORY_INELIGIBLE_UNVERIFIED" in _reasons(fakes)


async def test_a_territory_claim_without_a_territory_read_is_vetoed(fakes, guardian_config, signing_seed):
    service, proposal = _territory_world(
        fakes,
        guardian_config,
        signing_seed,
        ObligationMarket("REGULATED", "AUSTIN_ENERGY", "REGULATED_CAPACITY"),
        reason="R-TERRITORY-INELIGIBLE",
    )
    service.ports = replace(service.ports, territory=None)

    verdict = await service.evaluate_and_sign(make_batch_row(proposal))

    assert "G-19" in verdict.vetoed_rule_ids


# --- manual operator targets (MANUAL_TARGET, R-OPERATOR-OVERRIDE, R-MANUAL-RAMP) ----------------------------


class _ManualTargets:
    def __init__(self, hubs: set[str], *, fail: bool = False) -> None:
        self.hubs, self.fail = hubs, fail

    async def manual_target_hubs(self, hub_ids):
        if self.fail:
            raise RuntimeError("malformed expires_at")
        return {h for h in hub_ids if h in self.hubs}


def _manual_world(
    fakes, config, seed, reason: str, targets: _ManualTargets | None, *, frozen_kw: str = "8.0"
):
    """An 8 kW commitment granted 2 kW; members hub-a and hub-b, 5 kW each at 25 C (10 kW together)."""
    proposal, _ = _reduced(fakes, reason, frozen_kw=frozen_kw)
    _members(fakes, {"hub-a": _hub(temp_c=25.0), "hub-b": _hub(temp_c=25.0)})
    service = service_with(fakes, config, seed)
    service.ports = replace(fakes.as_ports(), manual_targets=targets)
    return service, proposal


async def test_operator_override_is_signed_when_a_live_target_took_the_capability(
    fakes, guardian_config, signing_seed
):
    """hub-a is operator-owned: without it the bank offers 5 kW < 8 kW committed, so R-OPERATOR-OVERRIDE passes."""
    service, proposal = _manual_world(
        fakes, guardian_config, signing_seed, "R-OPERATOR-OVERRIDE", _ManualTargets({"hub-a"})
    )
    assert "G-19" not in (await service.evaluate_and_sign(make_batch_row(proposal))).vetoed_rule_ids


async def test_operator_override_without_a_live_target_is_vetoed(fakes, guardian_config, signing_seed):
    service, proposal = _manual_world(
        fakes, guardian_config, signing_seed, "R-OPERATOR-OVERRIDE", _ManualTargets(set())
    )
    verdict = await service.evaluate_and_sign(make_batch_row(proposal))
    assert "G-19" in verdict.vetoed_rule_ids and "OPERATOR_OVERRIDE_NO_LIVE_TARGET" in _reasons(fakes)


async def test_operator_override_when_the_rest_of_the_bank_could_deliver_is_vetoed(
    fakes, guardian_config, signing_seed
):
    service, proposal = _manual_world(
        fakes,
        guardian_config,
        signing_seed,
        "R-OPERATOR-OVERRIDE",
        _ManualTargets({"hub-a"}),
        frozen_kw="4.0",
    )
    verdict = await service.evaluate_and_sign(make_batch_row(proposal))
    assert "G-19" in verdict.vetoed_rule_ids and "OPERATOR_OVERRIDE_UNVERIFIED" in _reasons(fakes)


async def test_a_failed_manual_target_read_is_never_evidence(fakes, guardian_config, signing_seed):
    service, proposal = _manual_world(
        fakes, guardian_config, signing_seed, "R-OPERATOR-OVERRIDE", _ManualTargets({"hub-a"}, fail=True)
    )
    assert "G-19" in (await service.evaluate_and_sign(make_batch_row(proposal))).vetoed_rule_ids


async def test_an_infeasible_claim_is_corroborated_without_the_operator_owned_hubs(
    fakes, guardian_config, signing_seed
):
    """The allocator treats operator-owned hubs as unavailable: its INFEASIBLE claim (10 kW bank, 8 kW floor)
    is true once hub-a's live target is taken into account, and false without it."""
    service, proposal = _manual_world(
        fakes, guardian_config, signing_seed, "R-COMMIT-LOCK-INFEASIBLE", _ManualTargets({"hub-a"})
    )
    assert "G-19" not in (await service.evaluate_and_sign(make_batch_row(proposal))).vetoed_rule_ids

    no_target, proposal2 = _manual_world(
        fakes, guardian_config, signing_seed, "R-COMMIT-LOCK-INFEASIBLE", None
    )
    assert "G-19" in (await no_target.evaluate_and_sign(make_batch_row(proposal2))).vetoed_rule_ids


async def test_manual_ramp_items_are_signed_under_the_ordinary_item_checks(
    fakes, guardian_config, signing_seed
):
    """R-MANUAL-RAMP items carry no obligation: nothing in G-19 applies, G-01/G-02/G-04 check them per item."""
    ok = replace(make_proposal(), items=[ProposedItem("hub-0001", 3.0, "R-MANUAL-RAMP")])
    wire_default_passing_scenario(fakes, ok)
    assert (
        await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(make_batch_row(ok))
    ).outcome == "PASS"

    over = replace(make_proposal(seq=2), items=[ProposedItem("hub-0001", 999.0, "R-MANUAL-RAMP")])
    wire_default_passing_scenario(fakes, over)
    verdict = await service_with(fakes, guardian_config, signing_seed).evaluate_and_sign(make_batch_row(over))
    assert "G-02" in verdict.vetoed_rule_ids and verdict.signature is None


def test_an_operator_override_shortfall_is_a_traced_k13_exception():
    """The engine traces and records R-OPERATOR-OVERRIDE shortfalls as K13 lock exceptions, like the others."""
    from opengrid.core.reasons import R_OPERATOR_OVERRIDE
    from opengrid.engine.gateways import _K13_SHORTFALL_REASONS

    assert R_OPERATOR_OVERRIDE in _K13_SHORTFALL_REASONS
