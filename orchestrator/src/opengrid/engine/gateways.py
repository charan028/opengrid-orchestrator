"""Concrete `opengrid.allocator.gateways` adapters wiring the allocator's 2 s cycle to
`opengrid.fleet`/`opengrid.ledger`/`og.feed_obs` (dispatch-live pass, merge task item 2).

Why this lives in `opengrid.engine`: `opengrid.allocator`'s own docstring (`allocator/gateways.py`) is
explicit that "production wiring (owned by `engine`, 02b S1.2) supplies real gateways" -- the allocator's
pure cycle logic and its thin I/O adapter (`run_cycle`) must never import `opengrid.fleet`'s Postgres
backend or hold a connection pool themselves (BUILD.md S5a "pure logic separated from I/O"). This module
is that engine-owned wiring layer, called once per 2 s tick from `opengrid.engine._engine_tick`.

Price/schedule data is read directly from `og.feed_obs` here rather than through `opengrid.feeds.latest`
because `opengrid.feeds` is a *separate process* (`og-feeds`) with its own private `FeedStore` singleton
-- `opengrid.feeds.latest()` would raise `RuntimeError` if called from `og-engine`'s process, since
nothing ever calls `opengrid.feeds.run_feeds_process()` there. Reading the same table both processes
share is the correct cross-process boundary (mirrors `opengrid.guardian.repo.PgBankStatePort` reading
`og.feed_obs` directly for the identical reason).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg_pool import AsyncConnectionPool

from opengrid import contracts, fleet, ledger
from opengrid.allocator.energy_sufficiency import (
    EnergySufficiencyResult,
    HubEnergyState,
    evaluate_with_substitution,
)
from opengrid.allocator.models import (
    BankSnapshot,
    FleetState,
    HubSnapshot,
    Instruction,
    LedgerView,
    ObligationCall,
    PriceSignal,
    ProposedGrant,
    ScadaSample,
    Schedule,
    SubstitutionEvent,
)
from opengrid.core.models.mqtt import ScadaUtilityInstruction
from opengrid.core.reasons import ALR_ENERGY_SHORTFALL_RISK, R_SUBSTITUTION
from opengrid.health.model import AlertFinding
from opengrid.health.queries import raise_alert
from opengrid.health.rules import evaluate_energy_shortfall_risk_alert
from opengrid.ledger import GrantRecord
from opengrid.trace import TraceStore

logger = logging.getLogger(__name__)

# item 3's continuous energy-sufficiency check: every COMMITTED/DELIVERING obligation's remaining
# committed draw against its bank(s), joined to the contract for customer_id and the obligation for
# window_end. MVP-S reservations are bank-scoped (02a S1.9), so this check is scoped to (obligation,
# bank) pairs exactly like `_ACTIVE_CALLS_SQL` above -- the same simplification already used for S1-S7.
_UPSERT_ENERGY_STATUS_SQL = """
INSERT INTO og.obligation_energy_status
    (obligation_id, required_kwh, available_kwh, margin_kwh, time_to_depletion_h, at_risk,
     used_substitution, computed_at)
VALUES (%(obligation_id)s, %(required_kwh)s, %(available_kwh)s, %(margin_kwh)s,
        %(time_to_depletion_h)s, %(at_risk)s, %(used_substitution)s, now())
ON CONFLICT (obligation_id) DO UPDATE SET
    required_kwh = EXCLUDED.required_kwh, available_kwh = EXCLUDED.available_kwh,
    margin_kwh = EXCLUDED.margin_kwh, time_to_depletion_h = EXCLUDED.time_to_depletion_h,
    at_risk = EXCLUDED.at_risk, used_substitution = EXCLUDED.used_substitution,
    computed_at = EXCLUDED.computed_at
"""

# Per (obligation, bank): the committed energy still to deliver (kWh = sum of each remaining 15-min
# reservation's kW x its not-yet-elapsed hours) and when that draw ends. Only obligations delivering now
# or starting within the look-ahead are checked -- a delivery hours away has time to recharge, and
# summing a whole day of sequential deliveries against today's SoC raised thousands of false alerts
# (live 2026-09-26: 6,748 ALR-ENERGY-SHORTFALL-RISK rows in 6 minutes, stalling the engine tick).
_ENERGY_SUFFICIENCY_ROWS_SQL = """
SELECT r.obligation_id, r.bank_id,
       SUM(r.amount * EXTRACT(EPOCH FROM (r.interval_end - GREATEST(r.interval_start, %(now)s))) / 3600.0)
           AS required_kwh,
       MAX(r.interval_end) AS draw_end,
       c.customer_id
FROM og.reservation r
JOIN og.obligation o ON o.obligation_id = r.obligation_id
JOIN og.contract c ON c.contract_id = o.contract_id
WHERE r.released_at IS NULL
  AND r.kind = 'POWER_KW'
  AND o.state IN ('COMMITTED', 'DELIVERING')
  AND o.window_start <= %(lookahead_end)s
  AND r.interval_end > %(now)s
GROUP BY r.obligation_id, r.bank_id, c.customer_id
"""

#: Obligations whose window starts within this many seconds are energy-checked ahead of delivery.
DEFAULT_ENERGY_LOOKAHEAD_S = 900.0

# 02b's "current price" signal for the allocator's headroom/dwell threshold (S6) -- ERCOT settlement
# point price (np6-905-cd, see opengrid.feeds.ercot's product map), the same product
# opengrid.api/opengrid.guardian already treat as the live price series. Not a hub-specific LMP -- MVP-S
# uses one system-wide price for the $/MWh threshold, per 02a S5.5's single hysteresis/hub.
_PRICE_PRODUCT = "np6-905-cd"

_LATEST_PRICE_SQL = """
SELECT value FROM og.feed_obs WHERE source = 'ERCOT' AND product = %(product)s
ORDER BY ts DESC LIMIT 1
"""

# Active (COMMITTED/DELIVERING) obligations' calls on the given banks for "now" (02a S1's active_calls):
# the reservation is the K13 frozen floor; obligation/opportunity give service_type/tier/value.
_ACTIVE_CALLS_SQL = """
SELECT r.obligation_id, r.bank_id, r.amount, o.service_type, o.tier, op.value_per_mwh
FROM og.reservation r
JOIN og.obligation o ON o.obligation_id = r.obligation_id
JOIN og.opportunity op ON op.opportunity_id = o.opportunity_id
WHERE r.bank_id = ANY(%(bank_ids)s)
  AND r.released_at IS NULL
  AND r.interval_start <= %(now)s AND r.interval_end > %(now)s
  AND o.state IN ('COMMITTED', 'DELIVERING')
"""

# Latest grant per obligation on these banks -- `prior_granted_kw` (02a S5.1's K13 "never below
# min(committed, prior_granted)" floor). DISTINCT ON picks the most recent row per obligation.
_PRIOR_GRANTS_SQL = """
SELECT DISTINCT ON (obligation_id) obligation_id, granted_kw
FROM og.grant
WHERE bank_id = ANY(%(bank_ids)s) AND obligation_id IS NOT NULL
ORDER BY obligation_id, created_at DESC
"""

_HEALTH_TO_ALLOCATOR: dict[str, str] = {
    "online": "OK",
    "stale": "LAGGING",
    "offline": "STALE",
    "fault": "FAULT",
}


class FleetCapabilityProvider:
    """`opengrid.ledger.CapabilityProvider` backed by the fleet twin -- wires `ReservationLedger`'s K2
    one-buyer check to the same `fleet.capability()` the allocator/selector already read (merge task:
    `opengrid.ledger.configure()` was never called anywhere, so every `selector.run_gate` ->
    `ledger.reserve()` call raised `RuntimeError` before this)."""

    async def capability_kw(self, bank_id: str, interval_start: datetime) -> Decimal:
        cap = await fleet.capability(bank_id, interval_start)
        return Decimal(str(cap.max_discharge_kw))


class EngineFleetGateway:
    """`opengrid.allocator.gateways.FleetGateway` backed by the in-process fleet twin
    (`opengrid.fleet`, already loaded/kept warm by `og-engine`'s own `load_topology()`/MQTT ingest --
    no extra I/O here beyond what the twin already does)."""

    async def bank_ids(self) -> Sequence[str]:
        return fleet.known_bank_ids()

    async def fleet_state(self, bank_ids: Sequence[str], interval_start: datetime) -> FleetState:
        hubs: list[HubSnapshot] = []
        banks: list[BankSnapshot] = []
        for bank_id in bank_ids:
            try:
                cap = await fleet.capability(bank_id, interval_start)
            except LookupError:
                logger.warning("fleet_state: unknown bank_id, skipping", extra={"bank_id": bank_id})
                continue
            scada = fleet.bank_scada_signal(bank_id)
            load_kva = scada.value if scada is not None and scada.signal == "APPARENT_POWER_KVA" else 0.0
            banks.append(
                BankSnapshot(
                    bank_id=bank_id,
                    capability_kw=cap.max_discharge_kw,
                    load_kva=load_kva,
                    zone=fleet.bank_zone(bank_id),
                )
            )
            for snap in fleet.hub_capabilities(bank_id):
                hubs.append(
                    HubSnapshot(
                        hub_id=snap.hub_id,
                        bank_id=snap.bank_id,
                        free_discharge_kw=snap.free_discharge_kw,
                        health=_HEALTH_TO_ALLOCATOR.get(snap.health, "FAULT"),  # type: ignore[arg-type]
                        # CORE-003/K1 (user requirement: energy above reserve checked continuously, not
                        # just power headroom): live SoC/reserve/capacity/efficiency from the fleet
                        # twin's own telemetry, so the allocator's `_cap_sustainable_discharge` can cap
                        # `free_discharge_kw` by the ENERGY sustainable over the command's hold horizon.
                        # `snap.soc_kwh` is already `None` for any hub the twin excluded this instant
                        # (stale/offline/fault, `fleet._classify_health`) -- never a stale/missing
                        # reading silently forwarded as "trust free_discharge_kw at face value".
                        soc_kwh=snap.soc_kwh,
                        reserve_kwh=snap.reserve_kwh,
                        e_kwh=snap.e_kwh,
                        eta_d=snap.eta_d,
                    )
                )
        return FleetState(hubs=tuple(hubs), banks=tuple(banks))


class EngineScadaGateway:
    """`ScadaGateway` backed by `opengrid.fleet`'s stored latest SCADA readings (already ingested off
    MQTT by `og-engine`'s own subscriber, `opengrid.engine._mqtt_ingest_loop`)."""

    async def samples(self, bank_ids: Sequence[str]) -> dict[str, ScadaSample]:
        samples: dict[str, ScadaSample] = {}
        for bank_id in bank_ids:
            signal = fleet.bank_scada_signal(bank_id)
            if signal is None or signal.signal != "APPARENT_POWER_KVA":
                continue
            samples[bank_id] = ScadaSample(bank_id=bank_id, apparent_power_kva=signal.value)
        return samples


class EngineScheduleGateway:
    """`ScheduleGateway` reading the live price directly from `og.feed_obs` (see module docstring for
    why this doesn't go through `opengrid.feeds.latest`), and L2 instructions from the fleet twin's own
    ingest (`opengrid.fleet.utility_instruction`, already validated/stored off MQTT)."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def schedule(self, bank_ids: Sequence[str]) -> Schedule:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_LATEST_PRICE_SQL, {"product": _PRICE_PRODUCT})
            row = await cur.fetchone()
        if row is None:
            logger.warning("no live price observed yet; allocator sees price=0.0 this cycle")
            price = 0.0
        else:
            price = float(row[0])
        return Schedule(prices=tuple(PriceSignal(bank_id=b, price_usd_per_mwh=price) for b in bank_ids))

    async def instructions(self, bank_ids: Sequence[str]) -> Sequence[Instruction]:
        instructions: list[Instruction] = []
        for bank_id in bank_ids:
            instr: ScadaUtilityInstruction | None = fleet.utility_instruction(bank_id)
            if instr is None:
                continue
            instructions.append(
                Instruction(scope="BANK", scope_ref=bank_id, kind=instr.kind, limit_kw=instr.limit_kw)
            )
        return tuple(instructions)


class EngineLedgerGateway:
    """`LedgerGateway` backed by `og.reservation`/`og.obligation`/`og.opportunity` reads (this module's
    own SQL -- `opengrid.ledger`'s public interface is reservation-write-focused, not obligation-state
    read-focused, so a plain read query here does not duplicate any of its logic) plus
    `opengrid.ledger.persist_grants`/`ledger_version` for the writes (BUILD.md S1 "no duplicated
    functions": the actual grant-persistence logic lives in exactly one place, `opengrid.ledger`)."""

    def __init__(self, pool: AsyncConnectionPool, trace: TraceStore | None = None) -> None:
        self._pool = pool
        self._trace = trace

    async def ledger_view(self, bank_ids: Sequence[str], interval_start: datetime) -> LedgerView:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_ACTIVE_CALLS_SQL, {"bank_ids": list(bank_ids), "now": interval_start})
            call_rows = await cur.fetchall()
            await cur.execute(_PRIOR_GRANTS_SQL, {"bank_ids": list(bank_ids)})
            prior_rows = await cur.fetchall()

        prior_by_obligation = {str(obligation_id): float(kw) for obligation_id, kw in prior_rows}

        calls: list[ObligationCall] = []
        for obligation_id, bank_id, amount, service_type, tier, value_per_mwh in call_rows:
            try:
                eligible_hub_ids = tuple(
                    s.hub_id for s in fleet.hub_capabilities(bank_id) if s.health == "online"
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
                )
            )
        return LedgerView(calls=tuple(calls))

    async def ledger_version(self) -> int:
        return await ledger.ledger_version()

    async def persist_grants(self, cycle_id: str, grants: Sequence[ProposedGrant]) -> None:
        if not grants:
            return
        version = await ledger.ledger_version()
        records = [
            GrantRecord(
                grant_id=uuid5(NAMESPACE_URL, f"{cycle_id}:{g.bank_id}:{g.obligation_id}:{g.is_headroom}"),
                cycle_id=cycle_id,
                bank_id=g.bank_id,
                granted_kw=Decimal(str(round(g.granted_kw, 3))),
                ledger_version=version,
                obligation_id=UUID(g.obligation_id) if g.obligation_id else None,
                is_headroom=g.is_headroom,
            )
            for g in grants
        ]
        await ledger.persist_grants(cycle_id, records)

    async def record_substitution(
        self, obligation_id: str, from_hub_id: str, to_hub_id: str, reason_code: str
    ) -> None:
        """S5.3: a manual hub swap is recorded as a `SUBSTITUTION` trace event carrying its reason code
        (`og.grant` has no reason column; the trace is the audit record, 02a S8.1). The next cycle's
        grants realize the swap -- the obligation's commitment is never written (K13)."""
        await self._trace_substitution(
            {"obligation_id": obligation_id, "from_hub_ids": [from_hub_id], "to_hub_ids": [to_hub_id]},
            reason_code,
        )

    async def record_substitution_events(self, cycle_id: str, events: Sequence[SubstitutionEvent]) -> None:
        """S5.3: the automatic swaps one 2 s cycle made, one `SUBSTITUTION` trace event each."""
        for event in events:
            await self._trace_substitution(
                {
                    "cycle_id": cycle_id,
                    "obligation_id": event.obligation_id,
                    "bank_id": event.bank_id,
                    "from_hub_ids": list(event.from_hub_ids),
                    "to_hub_ids": list(event.to_hub_ids),
                },
                R_SUBSTITUTION,
            )

    async def _trace_substitution(self, payload: dict[str, object], reason_code: str) -> None:
        if self._trace is None:
            raise RuntimeError("EngineLedgerGateway was built without a TraceStore; substitutions need one")
        await self._trace.append(
            f"substitution-{payload['obligation_id']}",
            "SUBSTITUTION",
            "SUBSTITUTION",
            payload,
            reason_codes=[reason_code],
        )


class EnergySufficiencyGateway:
    """Continuous per-obligation ENERGY-sufficiency hook (K1, user requirement: energy above reserve
    checked continuously, not just power headroom). Runs every allocator cycle, independent of the S1-S7
    power-capability path (`opengrid.allocator.cycle`), for every COMMITTED/DELIVERING obligation.

    Reads live SoC from the fleet twin and treats OTHER obligations sharing the same bank's committed
    remaining energy as already-reserved (K2), distributed across the bank's online hubs proportional to
    `free_discharge_kw` (the same heuristic `opengrid.engine._distribute_hub_items` uses for the
    guardian hand-off) -- MVP-S reservations are bank-scoped, not hub-scoped (02a S1.9), so this is the
    finest granularity the schema actually supports; documented here rather than silently assumed.

    On AT_RISK (after trying substitution with the SAME bank's other online hubs first, S5.3's "hub
    substitution is always allowed"): traces the finding and raises `ALR-ENERGY-SHORTFALL-RISK`
    (`opengrid.health.queries.raise_alert`, the existing single writer of `og.alert` -- BUILD.md S1 "no
    duplicated functions"). Commitment-lock rules are untouched: this NEVER reallocates capacity to a
    different obligation (K13), it only observes and alerts.

    No AT_RISK obligation-lifecycle transition exists in `opengrid.contracts.state_machine` (`DELIVERING
    -> DELIVERING` requires `R-RENOM-GATE`, which this is not) -- per the build brief's own fallback,
    AT_RISK is recorded via trace + alert only; `at_risk` stays a state-machine concept for whoever adds
    a dedicated transition/reason code later.
    """

    def __init__(
        self, pool: AsyncConnectionPool, trace: TraceStore, *, lookahead_s: float = DEFAULT_ENERGY_LOOKAHEAD_S
    ) -> None:
        self._pool = pool
        self._trace = trace
        self._lookahead = timedelta(seconds=lookahead_s)
        # Obligations currently AT_RISK: the trace/alert is written on ENTRY only, not every 2 s cycle.
        self._at_risk: set[str] = set()

    async def run(self, now: datetime) -> list[EnergySufficiencyResult]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _ENERGY_SUFFICIENCY_ROWS_SQL, {"now": now, "lookahead_end": now + self._lookahead}
            )
            rows = await cur.fetchall()

        # (obligation_id, average kW over the remaining draw, draw end, customer_id) per bank.
        by_bank: dict[str, list[tuple[str, float, datetime, str | None]]] = {}
        for obligation_id, bank_id, required_kwh, draw_end, customer_id in rows:
            remaining_h = max((draw_end - now).total_seconds(), 0.0) / 3600.0
            avg_kw = float(required_kwh) / remaining_h if remaining_h > 0 else 0.0
            by_bank.setdefault(bank_id, []).append(
                (str(obligation_id), avg_kw, draw_end, str(customer_id) if customer_id else None)
            )

        results = await self._evaluate_banks(by_bank, now)
        now_at_risk = {r.obligation_id for r in results if r.at_risk}
        for obligation_id in self._at_risk - now_at_risk:
            await self._set_at_risk(obligation_id, False)
        self._at_risk &= now_at_risk  # recovered obligations may alert again on a later entry
        return results

    @staticmethod
    async def _set_at_risk(
        obligation_id: str, at_risk: bool, payload: dict[str, object] | None = None
    ) -> None:
        """Mirror the check onto `og.obligation.at_risk` (UI/API/settle read it). Best effort: the trace
        and alert are the safety record; a failed flag write never blocks the cycle."""
        try:
            await contracts.set_obligation_at_risk(
                UUID(obligation_id), at_risk, reason_code=ALR_ENERGY_SHORTFALL_RISK, payload=payload
            )
        except Exception:
            logger.exception("failed to set obligation at_risk", extra={"obligation_id": obligation_id})

    async def _evaluate_banks(
        self, by_bank: dict[str, list[tuple[str, float, datetime, str | None]]], now: datetime
    ) -> list[EnergySufficiencyResult]:

        results: list[EnergySufficiencyResult] = []
        for bank_id, obligations in by_bank.items():
            try:
                hub_snaps = fleet.hub_capabilities(bank_id)
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

            for obligation_id, committed_kw, window_end, customer_id in obligations:
                remaining_window_h = max((window_end - now).total_seconds(), 0.0) / 3600.0

                # K2: distribute every OTHER obligation on this bank's remaining required energy across
                # the bank's online hubs, proportional to free_discharge_kw share -- the energy this
                # obligation may NOT count as available.
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
                await self._record_status(result)
                if result.at_risk and result.obligation_id not in self._at_risk:
                    self._at_risk.add(result.obligation_id)
                    await self._record_at_risk(result, customer_id)
        return results

    async def _record_status(self, result: EnergySufficiencyResult) -> None:
        """Persists EVERY cycle's result (not just AT_RISK ones) to `og.obligation_energy_status`, so
        `GET /og/api/dispatch/opportunities`/the dispatch SSE stream can show a live
        `energy_margin_kwh`/`time_to_depletion_h` for an obligation that is currently fine, not only a
        trace/alert trail for the ones that weren't (merge task item 5). Best-effort: a status-row write
        failure must never block the AT_RISK trace/alert path below, which is the safety-relevant one."""
        try:
            async with self._pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    _UPSERT_ENERGY_STATUS_SQL,
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
            logger.exception(
                "failed to persist obligation_energy_status", extra={"obligation_id": result.obligation_id}
            )

    async def _record_at_risk(self, result: EnergySufficiencyResult, customer_id: str | None) -> None:
        await self._set_at_risk(
            result.obligation_id, True, {"margin_kwh": result.margin_kwh, "required_kwh": result.required_kwh}
        )
        payload = {
            "obligation_id": result.obligation_id,
            "customer_id": customer_id,
            "margin_kwh": result.margin_kwh,
            "required_kwh": result.required_kwh,
            "available_kwh": result.available_kwh,
            "time_to_depletion_h": result.time_to_depletion_h,
            "used_substitution": result.used_substitution,
        }
        await self._trace.append(
            f"energy-sufficiency-{result.obligation_id}",
            "ALERT",
            "ENERGY_SHORTFALL_RISK",
            payload,
            reason_codes=[ALR_ENERGY_SHORTFALL_RISK],
        )
        finding: AlertFinding = evaluate_energy_shortfall_risk_alert(
            obligation_id=result.obligation_id,
            customer_id=customer_id,
            margin_kwh=result.margin_kwh,
            time_to_depletion_h=result.time_to_depletion_h,
        )
        try:
            await raise_alert(self._pool, finding, opened_at=datetime.now(tz=UTC))
        except Exception:
            logger.exception(
                "failed to raise ALR-ENERGY-SHORTFALL-RISK alert",
                extra={"obligation_id": result.obligation_id},
            )


def build_gateways(
    pool: AsyncConnectionPool, trace: TraceStore | None = None
) -> tuple[EngineFleetGateway, EngineLedgerGateway, EngineScadaGateway, EngineScheduleGateway]:
    """Convenience constructor for `opengrid.engine.main`: one of each gateway, built once per process
    (the fleet/scada gateways hold no state of their own; the ledger/schedule gateways hold the shared
    pool, and the ledger gateway the process's trace store for substitution events)."""
    return (
        EngineFleetGateway(),
        EngineLedgerGateway(pool, trace),
        EngineScadaGateway(),
        EngineScheduleGateway(pool),
    )
