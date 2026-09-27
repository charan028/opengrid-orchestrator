# mypy: ignore-errors
"""ORACLE -- do not edit. The base commit's (main b3e01fe) versions of every function the r3.4.3 PERF-OPT work
changed, kept verbatim as the reference the optimized code must match byte for byte
(`test_alloc_perf_equivalence.py`). Module-level names of their home modules are reached through the module
(`_fleet.`, `gw.`), so the harness's clock and I/O patches apply to both versions alike.

`oracle()` installs them (and the verbatim old `allocator.cycle`, `alloc_perf_oracle_cycle.py`) for the
duration of a `with` block.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

from opengrid import allocator
from opengrid import fleet as _fleet
from opengrid.allocator import energy_sufficiency as es
from opengrid.allocator.energy_hold import deliverable_margin_kwh
from opengrid.allocator.energy_sufficiency import EnergySufficiencyResult, HubEnergyState
from opengrid.allocator.models import BankSnapshot, FleetState, HubSnapshot, LedgerView, ObligationCall
from opengrid.engine import gateways as gw
from opengrid.market.territory import FREE

from . import alloc_perf_oracle_cycle


# ------------------------------------------------------------------------------------ opengrid.fleet
async def capability(bank_id: str, interval_start: datetime) -> _fleet.AvailableCapability:
    bank_rt = _fleet._banks.get(bank_id)
    if bank_rt is None:
        raise LookupError(f"unknown bank_id: {bank_id}")

    now = _fleet.datetime.now(UTC)
    excluded: set[str] = set()
    discharge_kw: list[float] = []
    charge_kw = 0.0

    for hub_id in bank_rt.hub_ids:
        runtime = _fleet._hubs[hub_id]
        classification = _fleet.classify_hub_health(
            fault_code=runtime.fault_code,
            last_seen_at=runtime.last_seen_at,
            now=now,
            thresholds=_fleet._thresholds,
        )
        if classification != "online":
            excluded.add(hub_id)
            continue
        d_kw, c_kw = _fleet.hub_capability(runtime.soc_kwh, runtime.params)
        discharge_kw.append(d_kw)
        charge_kw += c_kw

    max_discharge_kw = _fleet.bank_capability(discharge_kw, bank_rt.params)

    scada = _fleet._bank_scada.get((bank_id, "APPARENT_POWER_KVA"))
    bank_load_kva = scada.value if scada is not None else 0.0
    headroom_kw = _fleet.recharge_headroom(bank_load_kva, bank_rt.params)
    max_charge_kw = min(charge_kw, headroom_kw)

    utility_limit = _fleet._active_utility_limit_kw(bank_id, now=now)
    if utility_limit is not None:
        max_discharge_kw = min(max_discharge_kw, utility_limit)
        max_charge_kw = min(max_charge_kw, utility_limit)

    return _fleet.AvailableCapability(
        bank_id=bank_id,
        interval_start=interval_start,
        max_discharge_kw=max_discharge_kw,
        max_charge_kw=max_charge_kw,
        excluded_hub_ids=frozenset(excluded),
    )


def hub_capabilities(bank_id: str) -> list[_fleet.HubCapabilitySnapshot]:
    bank_rt = _fleet._banks.get(bank_id)
    if bank_rt is None:
        raise LookupError(f"unknown bank_id: {bank_id}")

    now = _fleet.datetime.now(UTC)
    snapshots: list[_fleet.HubCapabilitySnapshot] = []
    for hub_id in bank_rt.hub_ids:
        runtime = _fleet._hubs[hub_id]
        classification = _fleet.classify_hub_health(
            fault_code=runtime.fault_code,
            last_seen_at=runtime.last_seen_at,
            now=now,
            thresholds=_fleet._thresholds,
        )
        free_discharge_kw = 0.0
        soc_kwh: float | None = None
        reserve_kwh: float | None = None
        e_kwh: float | None = None
        p_kw: float | None = None
        if classification == "online":
            free_discharge_kw, _charge_kw = _fleet.hub_capability(runtime.soc_kwh, runtime.params)
            soc_kwh = runtime.soc_kwh
            reserve_kwh = runtime.params.r_kwh
            e_kwh = runtime.params.e_kwh
            p_kw = runtime.p_kw
        flow = runtime.flow if classification == "online" else {}
        snapshots.append(
            _fleet.HubCapabilitySnapshot(
                hub_id=hub_id,
                bank_id=bank_id,
                free_discharge_kw=free_discharge_kw,
                health=classification,
                last_seen_at=runtime.last_seen_at,
                soc_kwh=soc_kwh,
                reserve_kwh=reserve_kwh,
                e_kwh=e_kwh,
                eta_d=runtime.params.eta_d,
                p_kw=p_kw,
                ramp_kw_per_s=_fleet.hub_ramp_kw_per_s(runtime.params),
                rated_kw=runtime.params.p_kw,
                home_load_kw=flow.get("home_load_kw"),
                pv_kw=flow.get("pv_kw"),
                meter_kw=flow.get("meter_kw"),
                cell_temp_c=flow.get("cell_temp_c"),
                p_dis_max_kw=flow.get("p_dis_max_kw"),
                p_ch_max_kw=flow.get("p_ch_max_kw"),
                peak_power_budget_kws=flow.get("peak_power_budget_kws"),
                units=runtime.params.units,
                utility_scale=runtime.params.utility_scale,
            )
        )
    return snapshots


# ------------------------------------------------------------------------ opengrid.engine.gateways
async def fleet_state(self, bank_ids: Sequence[str], interval_start: datetime) -> FleetState:
    hubs: list[HubSnapshot] = []
    banks: list[BankSnapshot] = []
    for bank_id in bank_ids:
        try:
            cap = await gw.fleet.capability(bank_id, interval_start)
        except LookupError:
            gw.logger.warning("fleet_state: unknown bank_id, skipping", extra={"bank_id": bank_id})
            continue
        scada = gw.fleet.bank_scada_signal(bank_id, "APPARENT_POWER_KVA")
        load_kva = scada.value if scada is not None else 0.0
        territory = self._market.territory_of_bank(bank_id) if self._market is not None else None
        banks.append(
            BankSnapshot(
                bank_id=bank_id,
                capability_kw=cap.max_discharge_kw,
                load_kva=load_kva,
                zone=gw.fleet.bank_zone(bank_id),
                territory=territory,
                free_access=self._market.free_access(territory) if self._market is not None else False,
                available=not gw.is_unavailable_bank(bank_id),
            )
        )
        for snap in gw.fleet.hub_capabilities(bank_id):
            hubs.append(
                HubSnapshot(
                    hub_id=snap.hub_id,
                    bank_id=snap.bank_id,
                    free_discharge_kw=snap.free_discharge_kw,
                    health=gw._HEALTH_TO_ALLOCATOR.get(snap.health, "FAULT"),
                    soc_kwh=snap.soc_kwh,
                    reserve_kwh=snap.reserve_kwh,
                    e_kwh=snap.e_kwh,
                    eta_d=snap.eta_d,
                    rated_kw=snap.rated_kw,
                    p_kw=snap.p_kw,
                    cell_temp_c=getattr(snap, "cell_temp_c", None),
                    p_dis_max_kw=getattr(snap, "p_dis_max_kw", None),
                    meter_kw=getattr(snap, "meter_kw", None),
                    units=getattr(snap, "units", None),
                    utility_scale=bool(getattr(snap, "utility_scale", False))
                    or gw.is_utility_scale_bank(snap.bank_id),
                )
            )
    return FleetState(hubs=tuple(hubs), banks=tuple(banks))


async def ledger_view(self, bank_ids: Sequence[str], interval_start: datetime) -> LedgerView:
    async with self._pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(gw._ACTIVE_CALLS_SQL, {"bank_ids": list(bank_ids), "now": interval_start})
        call_rows = await cur.fetchall()
        await cur.execute(gw._PRIOR_GRANTS_SQL, {"bank_ids": list(bank_ids)})
        prior_rows = await cur.fetchall()
    markets = await self._markets([str(row[0]) for row in call_rows])
    on_unavailable = any(gw.is_unavailable_bank(str(row[1])) for row in call_rows)
    grandfathered = await self._grandfathered() if on_unavailable else set()

    prior_by_obligation = {str(obligation_id): float(kw) for obligation_id, kw in prior_rows}
    call_scale = gw.called_kw_scale(call_rows)
    self._committed_kw = {}
    self._in_shortfall = set()
    for row in call_rows:
        oid = str(row[0])
        scaled_kw = float(row[2]) * call_scale.get(oid, 1.0)
        self._committed_kw[oid] = self._committed_kw.get(oid, 0.0) + scaled_kw
        if row[6] == "SHORTFALL":
            self._in_shortfall.add(oid)

    calls: list[ObligationCall] = []
    for row in call_rows:
        obligation_id, bank_id, amount, service_type, tier, value_per_mwh, state, as_deployed = row[:8]
        duration_minutes = row[8] if len(row) > 8 else None
        deploy_end = row[9] if len(row) > 9 else None
        amount = float(amount) * call_scale.get(str(obligation_id), 1.0)
        try:
            eligible_hub_ids = tuple(
                s.hub_id for s in gw.fleet.hub_capabilities(bank_id) if s.health == "online"
            )
        except LookupError:
            eligible_hub_ids = ()
        calls.append(
            ObligationCall(
                obligation_id=str(obligation_id),
                bank_id=bank_id,
                service_type=service_type,
                tier=tier,
                committed_kw=float(amount),
                eligible_hub_ids=eligible_hub_ids,
                prior_granted_kw=prior_by_obligation.get(str(obligation_id)),
                value_per_mwh=float(value_per_mwh) if value_per_mwh is not None else 0.0,
                in_shortfall=state == "SHORTFALL",
                as_deployed=bool(as_deployed),
                hold_duration_h=float(str(duration_minutes)) / 60.0 if duration_minutes else None,
                deployment_remaining_h=(
                    max((gw.to_utc(deploy_end) - gw.to_utc(interval_start)).total_seconds(), 0.0) / 3600.0
                    if deploy_end is not None and as_deployed
                    else None
                ),
                market_ref=markets.get(str(obligation_id), FREE),
                grandfathered=(str(obligation_id), str(bank_id)) in grandfathered,
                # r3.4.4 (DISPATCH): the base gateway tags a D-33 partial call.
                partial_call=gw.is_partial_call(as_deployed, str(obligation_id), call_scale),
            )
        )
    return LedgerView(calls=tuple(calls))


async def _evaluate_banks(self, by_bank, now: datetime) -> list[EnergySufficiencyResult]:
    results: list[EnergySufficiencyResult] = []
    for bank_id, obligations in by_bank.items():
        try:
            hub_snaps = gw.fleet.hub_capabilities(bank_id)
        except LookupError:
            continue
        online_hubs = [h for h in hub_snaps if h.health == "online"]
        total_free_kw = sum(h.free_discharge_kw for h in online_hubs)
        hub_states = [
            HubEnergyState(
                hub_id=h.hub_id,
                soc_kwh=h.soc_kwh,
                reserve_kwh=h.reserve_kwh if h.reserve_kwh is not None else 0.0,
                eta_d=h.eta_d,
            )
            for h in online_hubs
        ]
        hold_margin_kwh = deliverable_margin_kwh(
            (getattr(h, "e_kwh", None), h.eta_d) for h in online_hubs if h.soc_kwh is not None
        )

        for obligation_id, committed_kw, window_end, customer_id in obligations:
            remaining_window_h = max((window_end - now).total_seconds(), 0.0) / 3600.0
            if obligation_id in self._as_ids and remaining_window_h > 0:
                committed_kw += hold_margin_kwh / remaining_window_h

            reserved_kwh_by_hub: dict[str, float] = {}
            if total_free_kw > 0:
                for other_id, other_kw, other_window_end, _other_customer in obligations:
                    if other_id == obligation_id:
                        continue
                    other_remaining_h = max((other_window_end - now).total_seconds(), 0.0) / 3600.0
                    other_required_kwh = other_kw * other_remaining_h
                    for h in online_hubs:
                        share = other_required_kwh * (h.free_discharge_kw / total_free_kw)
                        reserved_kwh_by_hub[h.hub_id] = reserved_kwh_by_hub.get(h.hub_id, 0.0) + share

            result = evaluate_with_substitution(
                obligation_id,
                committed_kw,
                remaining_window_h,
                hub_states,
                hub_states,
                reserved_kwh_by_hub,
            )
            results.append(result)
            if result.at_risk and result.obligation_id not in self._at_risk:
                self._at_risk.add(result.obligation_id)
                await self._record_at_risk(result, customer_id)
    await self._record_statuses(results)
    return results


async def _record_statuses(self, results: list[EnergySufficiencyResult]) -> None:
    if not results:
        return
    try:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(gw._ASYNC_COMMIT_SQL)
            for result in results:
                await cur.execute(
                    gw._UPSERT_ENERGY_STATUS_SQL,
                    {
                        "obligation_id": result.obligation_id,
                        "required_kwh": result.required_kwh,
                        "available_kwh": result.available_kwh,
                        "margin_kwh": result.margin_kwh,
                        "time_to_depletion_h": result.time_to_depletion_h,
                        "at_risk": result.at_risk,
                        "used_substitution": result.used_substitution,
                    },
                )
            await conn.commit()
    except Exception:
        gw.logger.exception("failed to persist obligation_energy_status", extra={"count": len(results)})


# -------------------------------------------------------------- opengrid.allocator.energy_sufficiency
def evaluate_with_substitution(
    obligation_id: str,
    committed_kw: float,
    remaining_window_h: float,
    primary_hubs: Sequence[HubEnergyState],
    substitute_hubs: Sequence[HubEnergyState],
    reserved_kwh_by_hub_for_others: Any,
) -> EnergySufficiencyResult:
    primary_result = es.evaluate_energy_sufficiency(
        obligation_id, committed_kw, remaining_window_h, primary_hubs, reserved_kwh_by_hub_for_others
    )
    if not primary_result.at_risk or not substitute_hubs:
        return primary_result

    combined_hubs = list(primary_hubs) + [
        h for h in substitute_hubs if h.hub_id not in {p.hub_id for p in primary_hubs}
    ]
    combined_result = es.evaluate_energy_sufficiency(
        obligation_id, committed_kw, remaining_window_h, combined_hubs, reserved_kwh_by_hub_for_others
    )
    if combined_result.at_risk:
        return combined_result
    return EnergySufficiencyResult(
        obligation_id=combined_result.obligation_id,
        required_kwh=combined_result.required_kwh,
        available_kwh=combined_result.available_kwh,
        margin_kwh=combined_result.margin_kwh,
        time_to_depletion_h=combined_result.time_to_depletion_h,
        at_risk=False,
        used_substitution=True,
    )


# -------------------------------------------------------------------------------------- allocator
def _old_cycle(*args: Any, prep_memo: Any = None, **kwargs: Any) -> Any:
    """The old `cycle()` (it had no `prep_memo`; `run_cycle` passing one is the only change there)."""
    return alloc_perf_oracle_cycle.cycle(*args, **kwargs)


@contextlib.contextmanager
def oracle() -> Iterator[None]:
    """Install the base commit's versions of every changed function."""
    patches = (
        (_fleet, "capability", capability),
        (_fleet, "hub_capabilities", hub_capabilities),
        (gw.EngineFleetGateway, "fleet_state", fleet_state),
        (gw.EngineLedgerGateway, "ledger_view", ledger_view),
        (gw.EnergySufficiencyGateway, "_evaluate_banks", _evaluate_banks),
        (gw.EnergySufficiencyGateway, "_record_statuses", _record_statuses),
        (es, "evaluate_with_substitution", evaluate_with_substitution),
        (allocator, "cycle", _old_cycle),
    )
    saved = [(target, name, getattr(target, name)) for target, name, _ in patches]
    try:
        for target, name, value in patches:
            setattr(target, name, value)
        yield
    finally:
        for target, name, value in saved:
            setattr(target, name, value)
