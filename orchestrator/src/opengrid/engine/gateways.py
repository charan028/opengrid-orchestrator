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
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg_pool import AsyncConnectionPool

from opengrid import contracts, fleet, ledger
from opengrid.allocator.energy_hold import (
    DEFAULT_AS_DEPLOYMENT_H,
    HOLD_MARGIN_FRACTION,
    deliverable_margin_kwh,
)
from opengrid.allocator.energy_sufficiency import (
    EnergySufficiencyResult,
    HubEnergyState,
    evaluate_with_substitution,
)
from opengrid.allocator.models import (
    HOLD_SERVICE_TYPES,
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
    ShortfallReport,
    SubstitutionEvent,
)
from opengrid.core.economics import wear_cost
from opengrid.core.models.mqtt import ScadaUtilityInstruction
from opengrid.core.physics import DEFAULT_ETA_C, DEFAULT_ETA_D
from opengrid.core.reasons import (
    ALR_ENERGY_SHORTFALL_RISK,
    COMMIT_LOCK_OVERRIDE_REASONS,
    LOCK_REASON_BY_SHORTFALL,
    R_OPERATOR_OVERRIDE,
    R_SHORTFALL_RESTORED,
    R_SUBSTITUTION,
)
from opengrid.core.timeutil import floor_to_interval, to_utc
from opengrid.engine.alerts import clear_open_alerts, open_alert_details
from opengrid.health.model import AlertFinding
from opengrid.health.queries import raise_alert
from opengrid.health.rules import evaluate_energy_shortfall_risk_alert
from opengrid.ledger import GrantRecord
from opengrid.market.availability import (
    BANK_AVAILABILITY_SQL,
    GRANDFATHERED_SQL,
    parse_availability,
    unavailable_bank_ids,
)
from opengrid.market.config import load_zone_territory
from opengrid.market.model import MarketModel
from opengrid.market.territory import FREE, MarketModelError, MarketRef, market_of
from opengrid.settle.tariffs import (
    load_tdsp_tariffs,
    resolve_tariff,
    resolve_tdsp_tariffs_path,
    tdsp_for_zone,
)
from opengrid.trace import TraceStore

logger = logging.getLogger(__name__)

#: A shortfall with one of these reasons is a K13 exception and must be traced (K13's own exception list),
#: including a reduction because a live operator target took the obligation's hubs (R-OPERATOR-OVERRIDE,
#: corroborated by the guardian's G-19 on its own MANUAL_TARGET read).
_K13_SHORTFALL_REASONS = COMMIT_LOCK_OVERRIDE_REASONS | {R_OPERATOR_OVERRIDE}

# item 3's continuous energy-sufficiency check: every COMMITTED/DELIVERING obligation's remaining
# committed draw against its bank(s), joined to the contract for customer_id and the obligation for
# window_end. MVP-S reservations are bank-scoped (02a S1.9), so this check is scoped to (obligation,
# bank) pairs exactly like `_ACTIVE_CALLS_SQL` above -- the same simplification already used for S1-S7.
#: Display state recomputed every cycle: a lost last commit on a crash costs nothing (same reasoning as
#: `opengrid.fleet.pg_backend`'s soft-state writes).
_ASYNC_COMMIT_SQL = "SET LOCAL synchronous_commit TO OFF"

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
       c.customer_id,
       o.service_type,
       -- ERCOT_AS energy hold: the award's kW now (or at its start within the look-ahead) and the
       -- product's full-deployment duration (Non-Spin 240 min, ECRS 60 min: product_rule.duration_minutes).
       MAX(r.amount) FILTER (WHERE r.interval_start <= %(lookahead_end)s) AS hold_kw,
       (SELECT MAX(pr.duration_minutes) FROM og.product_rule pr WHERE pr.contract_id = o.contract_id)
           AS duration_minutes,
       EXISTS (
           SELECT 1 FROM og.as_deployment d
           WHERE d.start_at <= now() AND d.end_at > now() AND d.cancelled_at IS NULL
             -- A NULL obligation_id means every ERCOT_AS award, never a utility toll (D-29).
             AND (d.obligation_id = r.obligation_id OR (d.obligation_id IS NULL AND o.service_type = 'ERCOT_AS'))
       ) AS as_deployed,
       -- The active deployment's end: while deployed the energy need is the rest of THIS call.
       (
           SELECT MAX(d.end_at) FROM og.as_deployment d
           WHERE d.start_at <= now() AND d.end_at > now() AND d.cancelled_at IS NULL
             AND (d.obligation_id = r.obligation_id OR (d.obligation_id IS NULL AND o.service_type = 'ERCOT_AS'))
       ) AS deploy_end
FROM og.reservation r
JOIN og.obligation o ON o.obligation_id = r.obligation_id
JOIN og.contract c ON c.contract_id = o.contract_id
WHERE r.released_at IS NULL
  AND r.kind = 'POWER_KW'
  -- SHORTFALL keeps delivering best-effort until its window ends (owner decision 2026-09-26)
  AND o.state IN ('COMMITTED', 'DELIVERING', 'SHORTFALL')
  AND o.window_start <= %(lookahead_end)s
  AND r.interval_end > %(now)s
GROUP BY r.obligation_id, r.bank_id, c.customer_id, o.service_type, o.contract_id
"""

#: Full-deployment duration assumed for an ERCOT_AS award whose product rule has none (ECRS, the shortest).
DEFAULT_AS_DEPLOYMENT_MINUTES = int(DEFAULT_AS_DEPLOYMENT_H * 60)
#: The guardian's G-01-ENERGY keeps this fraction of each hub's capacity above reserve at lease end; an AS
#: energy hold must cover it too (the one constant, `allocator.energy_hold`).
AS_HOLD_FLOOR_FRACTION = HOLD_MARGIN_FRACTION


def as_energy_hold(
    now: datetime, hold_kw: object, duration_minutes: object, deploy_end: datetime | None = None
) -> tuple[float, datetime]:
    """`(kW, draw end)` of a capacity hold's energy need (ERCOT_AS, D-29 toll): its committed kW sustained
    for the product's full deployment duration from now while HELD; while DEPLOYED only for the rest of the
    active call (`deploy_end - now`, capped by the product duration) -- sizing a call already under way
    at the full duration escalated a correctly delivering award to SHORTFALL (review R3).
    `evaluate_energy_sufficiency` then requires `kW x duration` of deliverable energy."""
    minutes = float(str(duration_minutes)) if duration_minutes else float(DEFAULT_AS_DEPLOYMENT_MINUTES)
    if deploy_end is not None:
        remaining_min = max((to_utc(deploy_end) - to_utc(now)).total_seconds(), 0.0) / 60.0
        minutes = min(minutes, remaining_min)
    return float(str(hold_kw)) if hold_kw else 0.0, now + timedelta(minutes=minutes)


#: Obligations whose window starts within this many seconds are energy-checked ahead of delivery.
DEFAULT_ENERGY_LOOKAHEAD_S = 900.0

# 02b's "current price" signal for the allocator's headroom/dwell threshold (S6) -- ERCOT settlement
# point price (np6-905-cd, see opengrid.feeds.ercot's product map), the same product
# opengrid.api/opengrid.guardian already treat as the live price series. Not a hub-specific LMP -- MVP-S
# uses one system-wide price for the $/MWh threshold, per 02a S5.5's single hysteresis/hub.
_PRICE_PRODUCT = "np6-905-cd"

# The latest real-time SPP per load zone (`series` = LZ_*): each bank is priced at its OWN zone. One price
# row for every bank (whichever zone happened to be written last) mispriced the whole fleet.
_LATEST_ZONE_PRICES_SQL = """
SELECT DISTINCT ON (series) series, value
FROM og.feed_obs
WHERE source = 'ERCOT' AND product = %(product)s AND ts > now() - interval '6 hours'
ORDER BY series, ts DESC, recorded_at DESC
"""

_CONSERVATIVE_SCOPES_SQL = "SELECT scope_kind, scope_ref FROM og.scope_posture WHERE posture = 'CONSERVATIVE'"

# The cheapest P50 price forecast per load zone over the next 24 h: the price at which a bank could
# recharge the energy a headroom discharge spends now.
_RECHARGE_PRICE_SQL = """
SELECT series_key, MIN(p50)
FROM og.forecast
WHERE kind = 'price' AND interval_start_utc >= now() AND interval_start_utc < now() + interval '24 hours'
GROUP BY series_key
"""

#: M1 TDSP delivery charge ($/MWh) on grid-drawn charging kWh, by the bank's load zone (09-optimizer-
#: dispatcher-update.md D5 / config tdsp_tariffs: Oncor 60.295, CNP 64.130, AEP Central 58.0, AEP North
#: 57.0 $/MWh). The zone -> TDSP correspondence is approximate (a load zone spans several TDSPs); an
#: unknown zone takes the highest charge, so a headroom discharge is never under-costed.
M1_USD_PER_MWH_BY_ZONE: dict[str, float] = {
    "LZ_NORTH": 60.295,
    "LZ_HOUSTON": 64.130,
    "LZ_SOUTH": 58.0,
    "LZ_WEST": 57.0,
}
_M1_FALLBACK_USD_PER_MWH = max(M1_USD_PER_MWH_BY_ZONE.values())


#: The home wear rate per AC kWh discharged (09 D8, A-DE-16: $0.03/kWh), `[allocator].wear_usd_per_kwh`.
DEFAULT_WEAR_USD_PER_KWH = 0.03

#: The optimizer's published stored-energy value read API (09 D7): `(bank_ids, now) -> {bank_id: $/MWh}`,
#: the break-even price for discharging one AC MWh of headroom now, `1000 x (nu / eta_d + wear)` (wear
#: included; `opengrid.selector.energy_value.discharge_threshold_usd_per_mwh`). A bank it has no value for
#: uses the replacement-cost fallback.
StoredEnergyValueReader = Callable[[Sequence[str], datetime], Awaitable[Mapping[str, float]]]
#: The selector's DB read of its published hard floors (`selector.db.load_hold_floors_kwh`).
HoldFloorReader = Callable[[list[str], datetime], Awaitable[Mapping[str, float]]]
HOLD_FLOOR_DB_REFRESH_S = 60.0


def wear_usd_per_mwh(wear_usd_per_kwh: float) -> float:
    """09 D8 wear per MWh discharged, via the one wear formula (`core.economics.wear_cost`)."""
    return float(wear_cost(Decimal(1000), Decimal(str(wear_usd_per_kwh))))


def headroom_threshold_usd_per_mwh(
    zone: str | None,
    recharge_price_usd_per_mwh: float | None,
    live_price_usd_per_mwh: float,
    *,
    round_trip_efficiency: float,
    wear_usd_per_mwh: float = 0.0,
    m1_by_zone: Mapping[str, float] | None = None,
) -> float:
    """Architect finding (c) / 09 S1.8 (G9) -- the fallback when the optimizer has published no stored-
    energy value for the bank: headroom is discharged only when the RT price covers what the spent
    energy costs to put back -- the cheapest recharge price ahead (else the live price) plus the M1
    delivery charge, grossed up for round-trip losses -- plus the wear of discharging it (D8). Never a
    fixed number. An unknown zone takes the highest M1 known, so headroom is never under-costed."""
    charge_price = (
        recharge_price_usd_per_mwh if recharge_price_usd_per_mwh is not None else live_price_usd_per_mwh
    )
    m1_table = M1_USD_PER_MWH_BY_ZONE if m1_by_zone is None else m1_by_zone
    fallback_m1 = max(m1_table.values(), default=_M1_FALLBACK_USD_PER_MWH)
    m1 = m1_table.get(zone or "", fallback_m1)
    return (max(charge_price, 0.0) + m1) / max(round_trip_efficiency, 1e-6) + max(wear_usd_per_mwh, 0.0)


def m1_usd_per_mwh_by_zone(as_of: date, *, config_path: str | None = None) -> dict[str, float]:
    """M1 ($/MWh on grid-charged kWh) per load zone from `tdsp_tariffs.toml` via `opengrid.settle.
    tariffs` (the file's owner): each competitive zone's default TDSP tariff in effect `as_of`, and 0 for a
    regulated-territory zone (`[zone_territory]`, 09 D5: no M1 inside AE/CPS). Raises `OSError` if the
    file is missing; the caller then keeps the built-in table."""
    path = resolve_tdsp_tariffs_path(config_path)
    tariffs, zone_default_tdsp = load_tdsp_tariffs(path)
    by_zone: dict[str, float] = {}
    for zone in zone_default_tdsp:
        tariff = resolve_tariff(tariffs, tdsp_for_zone(zone_default_tdsp, zone), as_of)
        if tariff is not None:
            by_zone[zone] = float(tariff.volumetric_usd_per_kwh) * 1000.0
    for zone in load_zone_territory(path):
        by_zone[zone] = 0.0
    return by_zone


def valid_value(value: object) -> float | None:
    """A published stored-energy value usable as a threshold: a finite number, else `None`."""
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _bank_zone(bank_id: str) -> str | None:
    try:
        return fleet.bank_zone(bank_id)
    except LookupError:
        return None


def bank_price(zone: str | None, price_by_zone: dict[str, float]) -> float:
    """A bank's live $/MWh: its own load zone's SPP; with no zone price, the mean of the zones seen (a
    documented fallback, never an arbitrary single zone); 0.0 when no price is observed at all."""
    if zone is not None and zone in price_by_zone:
        return price_by_zone[zone]
    load_zones = [v for k, v in price_by_zone.items() if k.startswith("LZ_")] or list(price_by_zone.values())
    return sum(load_zones) / len(load_zones) if load_zones else 0.0


# Active (COMMITTED/DELIVERING) obligations' calls on the given banks for "now" (02a S1's active_calls):
# the reservation is the K13 frozen floor; obligation/opportunity give service_type/tier/value.
_ACTIVE_CALLS_SQL = """
SELECT r.obligation_id, r.bank_id, r.amount, o.service_type, o.tier, op.value_per_mwh, o.state,
       -- ERCOT_AS is a capacity hold: it discharges only while ERCOT has deployed it (og.as_deployment,
       -- one obligation or every ERCOT_AS award when obligation_id is NULL).
       EXISTS (
           SELECT 1 FROM og.as_deployment d
           WHERE d.start_at <= now() AND d.end_at > now() AND d.cancelled_at IS NULL
             -- A NULL obligation_id means every ERCOT_AS award, never a utility toll (D-29).
             AND (d.obligation_id = o.obligation_id OR (d.obligation_id IS NULL AND o.service_type = 'ERCOT_AS'))
       ) AS as_deployed,
       -- The AS product's full-deployment duration (Non-Spin 240 min, ECRS 60 min) for its energy hold.
       (SELECT MAX(pr.duration_minutes) FROM og.product_rule pr WHERE pr.contract_id = o.contract_id)
           AS duration_minutes,
       (
           SELECT MAX(d.end_at) FROM og.as_deployment d
           WHERE d.start_at <= now() AND d.end_at > now() AND d.cancelled_at IS NULL
             AND (d.obligation_id = o.obligation_id OR (d.obligation_id IS NULL AND o.service_type = 'ERCOT_AS'))
       ) AS deploy_end
FROM og.reservation r
JOIN og.obligation o ON o.obligation_id = r.obligation_id
JOIN og.opportunity op ON op.opportunity_id = o.opportunity_id
WHERE r.bank_id = ANY(%(bank_ids)s)
  AND r.released_at IS NULL
  AND r.interval_start <= %(now)s AND r.interval_end > %(now)s
  -- Owner decision 2026-09-26: a mid-window SHORTFALL never stops dispatch; it keeps receiving the
  -- maximum feasible kW of its unchanged commitment until its window ends (K13: nothing reallocated).
  AND o.state IN ('COMMITTED', 'DELIVERING', 'SHORTFALL')
"""

#: 02a S2.2 commitment-lock event: a revision of the obligation's latest commitment row for the interval
#: covering now, with the lock reason and a `supersedes` link; plan, interval and kW copied unchanged.
_LOCK_EVENT_SQL = """
INSERT INTO og.commitment
    (commitment_id, obligation_id, plan_id, interval_start, interval_end, committed_kw, variable_kind,
     supersedes, reason_code)
SELECT gen_random_uuid(), c.obligation_id, c.plan_id, c.interval_start, c.interval_end, c.committed_kw,
       c.variable_kind, c.commitment_id, %(reason_code)s
FROM og.commitment c
WHERE c.obligation_id = %(obligation_id)s::uuid
  AND c.interval_start <= %(now)s AND c.interval_end > %(now)s
  AND NOT EXISTS (SELECT 1 FROM og.commitment n WHERE n.supersedes = c.commitment_id)
ORDER BY c.created_at DESC
LIMIT 1
"""


def lock_reason_of(shortfall_reason: str) -> str:
    """The K13 lock reason an allocator shortfall reason stands for (`R-SHORTFALL-*` detail codes map
    onto their override), for the commitment-lock event row."""
    return LOCK_REASON_BY_SHORTFALL.get(shortfall_reason, shortfall_reason)


_CONTRACT_MARKETS_SQL = """
SELECT o.obligation_id, c.market, c.utility_id, c.service_type
FROM og.obligation o
JOIN og.contract c ON c.contract_id = o.contract_id
WHERE o.obligation_id = ANY(%(obligation_ids)s::uuid[])
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


#: Banks carrying a utility-scale asset (og.asset SUBSTATION), refreshed with the market model: their
#: hubs are rated at nameplate, never at the home unit cap (`HubParams.utility_scale`).
_UTILITY_SCALE_BANKS: set[str] = set()

_SUBSTATION_BANKS_SQL = """
SELECT DISTINCT bank_id FROM og.asset WHERE asset_class = 'SUBSTATION' AND bank_id IS NOT NULL
"""


#: D-37: UNAVAILABLE banks (`og.bank.availability`, regulated with no contract), refreshed with the market
#: model: the allocator dispatches nothing on them except K13-grandfathered calls.
_UNAVAILABLE_BANKS: set[str] = set()


def set_unavailable_banks(bank_ids: set[str]) -> None:
    _UNAVAILABLE_BANKS.clear()
    _UNAVAILABLE_BANKS.update(bank_ids)


def is_unavailable_bank(bank_id: str) -> bool:
    return bank_id in _UNAVAILABLE_BANKS


async def load_unavailable_banks(pool: AsyncConnectionPool) -> set[str] | None:
    """D-37: the UNAVAILABLE banks (`market.availability`, one parser); `None` (logged) when unreadable (keep
    the last set; K15 still keeps FREE dispatch off regulated banks and the guardian re-checks G-33)."""
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(BANK_AVAILABILITY_SQL)
            rows = await cur.fetchall()
    except Exception:
        logger.error("og.bank availability unreadable; unavailable banks unchanged", exc_info=True)
        return None
    return set(unavailable_bank_ids(parse_availability(str(b), a, r, s) for b, a, r, s in rows))


def set_utility_scale_banks(bank_ids: set[str]) -> None:
    _UTILITY_SCALE_BANKS.clear()
    _UTILITY_SCALE_BANKS.update(bank_ids)


def is_utility_scale_bank(bank_id: str) -> bool:
    return bank_id in _UTILITY_SCALE_BANKS


async def load_utility_scale_banks(pool: AsyncConnectionPool) -> set[str] | None:
    """Banks with an og.asset SUBSTATION row; `None` (logged) when unreadable (keep the last set)."""
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_SUBSTATION_BANKS_SQL)
            rows = await cur.fetchall()
    except Exception:
        logger.warning("og.asset unreadable; utility-scale banks unchanged", exc_info=True)
        return None
    return {str(r[0]) for r in rows}


class EngineFleetGateway:
    """`opengrid.allocator.gateways.FleetGateway` backed by the in-process fleet twin
    (`opengrid.fleet`, already loaded/kept warm by `og-engine`'s own `load_topology()`/MQTT ingest --
    no extra I/O here beyond what the twin already does).

    With a `market_model`, each bank carries its K15 territory and whether that territory's utility
    grants FREE access (`opengrid.market`); without one, the territory is left unknown (the allocator
    enforces territory only when the engine's cycle extras turn it on)."""

    def __init__(self, market_model: MarketModel | None = None) -> None:
        self._market = market_model

    def set_market_model(self, market_model: MarketModel | None) -> None:
        """Swap in a rebuilt market model (periodic refresh); `None` keeps the current one."""
        if market_model is not None:
            self._market = market_model

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
            scada = fleet.bank_scada_signal(bank_id, "APPARENT_POWER_KVA")
            load_kva = scada.value if scada is not None else 0.0
            territory = self._market.territory_of_bank(bank_id) if self._market is not None else None
            banks.append(
                BankSnapshot(
                    bank_id=bank_id,
                    capability_kw=cap.max_discharge_kw,
                    load_kva=load_kva,
                    zone=fleet.bank_zone(bank_id),
                    territory=territory,
                    free_access=self._market.free_access(territory) if self._market is not None else False,
                    available=not is_unavailable_bank(bank_id),
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
                        rated_kw=snap.rated_kw,
                        p_kw=snap.p_kw,
                        # 09 S1.9 telemetry (migration 0027), picked up as soon as the fleet twin carries it;
                        # until then None: F1 takes the guardian's unknown-temperature factor.
                        cell_temp_c=getattr(snap, "cell_temp_c", None),
                        p_dis_max_kw=getattr(snap, "p_dis_max_kw", None),
                        meter_kw=getattr(snap, "meter_kw", None),
                        units=getattr(snap, "units", None),
                        utility_scale=bool(getattr(snap, "utility_scale", False))
                        or is_utility_scale_bank(snap.bank_id),
                    )
                )
        return FleetState(hubs=tuple(hubs), banks=tuple(banks))


class EngineScadaGateway:
    """`ScadaGateway` backed by `opengrid.fleet`'s stored latest SCADA readings (already ingested off
    MQTT by `og-engine`'s own subscriber, `opengrid.engine._mqtt_ingest_loop`)."""

    async def samples(self, bank_ids: Sequence[str]) -> dict[str, ScadaSample]:
        samples: dict[str, ScadaSample] = {}
        for bank_id in bank_ids:
            signal = fleet.bank_scada_signal(bank_id, "APPARENT_POWER_KVA")
            if signal is None:
                continue
            samples[bank_id] = ScadaSample(bank_id=bank_id, apparent_power_kva=signal.value)
        return samples


class EngineScheduleGateway:
    """`ScheduleGateway` reading the live price directly from `og.feed_obs` (see module docstring for
    why this doesn't go through `opengrid.feeds.latest`), and L2 instructions from the fleet twin's own
    ingest (`opengrid.fleet.utility_instruction`, already validated/stored off MQTT)."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        stored_energy_value: StoredEnergyValueReader | None = None,
        wear_usd_per_kwh: float = DEFAULT_WEAR_USD_PER_KWH,
        m1_by_zone: Mapping[str, float] | None = None,
        hold_floor_in_process: Callable[[str, datetime], float | None] = lambda bank_id, at: None,
        hold_floor_db: HoldFloorReader | None = None,
    ) -> None:
        self._pool = pool
        self._posture_warned = False
        self._stored_energy_value = stored_energy_value
        self._wear_usd_per_mwh = wear_usd_per_mwh(wear_usd_per_kwh)
        self._m1_by_zone = m1_by_zone
        self._value_warned = False
        self._hold_floor_in_process = hold_floor_in_process
        self._hold_floor_db = hold_floor_db
        self._db_floors: dict[str, float] = {}
        self._db_floors_at: datetime | None = None

    async def schedule(self, bank_ids: Sequence[str]) -> Schedule:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_LATEST_ZONE_PRICES_SQL, {"product": _PRICE_PRODUCT})
            zone_rows = await cur.fetchall()
        conservative = await self._conservative_scopes()
        recharge_by_zone = await self._recharge_prices()
        published = await self._published_values(bank_ids)
        price_by_zone = {str(series): float(value) for series, value in zone_rows}
        prices = []
        conservative_banks: set[str] = set()
        for bank_id in bank_ids:
            zone = _bank_zone(bank_id)
            live = bank_price(zone, price_by_zone)
            value = valid_value(published.get(bank_id))
            if zone is None or zone not in price_by_zone:
                # No live price for the bank's own zone: no headroom this cycle (threshold None), never
                # priced off other zones' prices or $0.
                prices.append(
                    PriceSignal(bank_id=bank_id, price_usd_per_mwh=live, threshold_usd_per_mwh=None)
                )
                if ("BANK", bank_id) in conservative or (zone is not None and ("ZONE", zone) in conservative):
                    conservative_banks.add(bank_id)
                continue
            # 09 D7: the optimizer's stored-energy break-even (wear included); else the replacement cost.
            threshold = (
                value
                if value is not None
                else headroom_threshold_usd_per_mwh(
                    zone,
                    recharge_by_zone.get(zone or ""),
                    live,
                    round_trip_efficiency=DEFAULT_ETA_C * DEFAULT_ETA_D,
                    wear_usd_per_mwh=self._wear_usd_per_mwh,
                    m1_by_zone=self._m1_by_zone,
                )
            )
            prices.append(
                PriceSignal(bank_id=bank_id, price_usd_per_mwh=live, threshold_usd_per_mwh=threshold)
            )
            if ("BANK", bank_id) in conservative or (zone is not None and ("ZONE", zone) in conservative):
                conservative_banks.add(bank_id)
        if not price_by_zone:
            logger.warning("no live price observed yet; allocator sees price=0.0 this cycle")
        return Schedule(
            prices=tuple(prices),
            conservative_bank_ids=frozenset(conservative_banks),
            hold_floor_kwh=await self._hold_floors(bank_ids),
        )

    async def _hold_floors(self, bank_ids: Sequence[str]) -> dict[str, float]:
        """09 S1.8 e^hold per bank: the selector's in-process value from the latest plan, else (after a
        restart, before this process has solved a plan) `selector.db.load_hold_floors_kwh`, read at most
        every `HOLD_FLOOR_DB_REFRESH_S`. Unreadable -> none (the AS energy hold still applies)."""
        now = datetime.now(UTC)
        floors: dict[str, float] = {}
        missing: list[str] = []
        for bank_id in bank_ids:
            value = self._hold_floor_in_process(bank_id, now)
            if value is not None:
                floors[bank_id] = value
            else:
                missing.append(bank_id)
        if missing and self._hold_floor_db is not None:
            due = (
                self._db_floors_at is None
                or (now - self._db_floors_at).total_seconds() >= HOLD_FLOOR_DB_REFRESH_S
            )
            if due:
                self._db_floors_at = now
                try:
                    self._db_floors = dict(await self._hold_floor_db(missing, now))
                except Exception:
                    logger.warning("stored hold floors unreadable; AS energy hold only", exc_info=True)
                    self._db_floors = {}
            floors.update({b: self._db_floors[b] for b in missing if b in self._db_floors})
        return floors

    async def _published_values(self, bank_ids: Sequence[str]) -> Mapping[str, float]:
        """The optimizer's stored-energy values; unreadable -> none (every bank falls back)."""
        if self._stored_energy_value is None:
            return {}
        try:
            return await self._stored_energy_value(bank_ids, datetime.now(UTC))
        except Exception:
            if not self._value_warned:
                logger.warning(
                    "stored-energy values unreadable; thresholds use the replacement cost", exc_info=True
                )
                self._value_warned = True
            return {}

    async def _recharge_prices(self) -> dict[str, float]:
        """Cheapest P50 price ahead per zone (og.forecast); unreadable/empty -> the live price is used."""
        try:
            async with self._pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(_RECHARGE_PRICE_SQL)
                rows = await cur.fetchall()
        except Exception:
            logger.warning("recharge-price forecast unreadable; thresholds use the live price", exc_info=True)
            return {}
        return {str(series): float(p50) for series, p50 in rows if p50 is not None}

    async def _conservative_scopes(self) -> set[tuple[str, str]]:
        """K7 escalation: the guardian's CONSERVATIVE scopes (og.scope_posture, migration 0019). Unreadable
        (e.g. before 0019 is applied) means none -- never a reason to stop committed dispatch."""
        try:
            async with self._pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(_CONSERVATIVE_SCOPES_SQL)
                rows = await cur.fetchall()
        except Exception:
            if not self._posture_warned:
                logger.warning("og.scope_posture unreadable; no CONSERVATIVE scopes applied", exc_info=True)
                self._posture_warned = True
            return set()
        return {(str(kind), str(ref)) for kind, ref in rows}

    async def instructions(self, bank_ids: Sequence[str]) -> Sequence[Instruction]:
        now = datetime.now(UTC)
        instructions: list[Instruction] = []
        for bank_id in bank_ids:
            instr: ScadaUtilityInstruction | None = fleet.utility_instruction(bank_id)
            if instr is None or not instruction_active(instr, now):
                continue
            instructions.append(
                Instruction(scope="BANK", scope_ref=bank_id, kind=instr.kind, limit_kw=instr.limit_kw)
            )
        return tuple(instructions)


def instruction_active(instr: ScadaUtilityInstruction, now: datetime) -> bool:
    """K5: an L2 instruction binds until its `expires_at`. Once the utility's constraint has expired the
    bank's full capability is back, so a best-effort obligation is restored to its full commitment on the
    next cycle (owner decision 2026-09-26) -- the latest stored instruction is not a standing cap."""
    return instr.expires_at is None or to_utc(instr.expires_at) > to_utc(now)


class EngineLedgerGateway:
    """`LedgerGateway` backed by `og.reservation`/`og.obligation`/`og.opportunity` reads (this module's
    own SQL -- `opengrid.ledger`'s public interface is reservation-write-focused, not obligation-state
    read-focused, so a plain read query here does not duplicate any of its logic) plus
    `opengrid.ledger.persist_grants`/`ledger_version` for the writes (BUILD.md S1 "no duplicated
    functions": the actual grant-persistence logic lives in exactly one place, `opengrid.ledger`)."""

    def __init__(self, pool: AsyncConnectionPool, trace: TraceStore | None = None) -> None:
        self._pool = pool
        self._trace = trace
        #: `(obligation_id, shortfall reason)` from the latest cycle, read by the engine's escalation.
        self.last_shortfalls: list[tuple[str, str]] = []
        #: (obligation_id, reason, interval) K13 shortfalls already traced in the current episode.
        self._traced_shortfalls: set[tuple[str, str, str]] = set()
        self._market_warned = False
        self._grandfather_warned = False
        #: This cycle's committed kW and SHORTFALL state per obligation (from `ledger_view`), and granted
        #: kW (from `persist_grants`), for the restore check.
        self._committed_kw: dict[str, float] = {}
        self._in_shortfall: set[str] = set()
        self._granted_kw: dict[str, float] = {}
        #: Obligations short (K13 exception) in the previous cycle, and those already traced restored.
        self._short_prev: set[str] = set()
        self._restored: set[str] = set()
        #: (obligation_id, reason) -> the 15-min interval its commitment-lock event was last written for.
        self._lock_events: dict[tuple[str, str], str] = {}

    async def ledger_view(self, bank_ids: Sequence[str], interval_start: datetime) -> LedgerView:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_ACTIVE_CALLS_SQL, {"bank_ids": list(bank_ids), "now": interval_start})
            call_rows = await cur.fetchall()
            await cur.execute(_PRIOR_GRANTS_SQL, {"bank_ids": list(bank_ids)})
            prior_rows = await cur.fetchall()
        markets = await self._markets([str(row[0]) for row in call_rows])
        # D-37/K13: grandfathering only matters on an UNAVAILABLE bank (the switched zones), so the read
        # runs only when a call sits on one.
        on_unavailable = any(is_unavailable_bank(str(row[1])) for row in call_rows)
        grandfathered = await self._grandfathered() if on_unavailable else set()

        prior_by_obligation = {str(obligation_id): float(kw) for obligation_id, kw in prior_rows}
        self._committed_kw = {}
        self._in_shortfall = set()
        for row in call_rows:
            oid = str(row[0])
            self._committed_kw[oid] = self._committed_kw.get(oid, 0.0) + float(row[2])
            if row[6] == "SHORTFALL":
                self._in_shortfall.add(oid)

        calls: list[ObligationCall] = []
        for row in call_rows:
            obligation_id, bank_id, amount, service_type, tier, value_per_mwh, state, as_deployed = row[:8]
            duration_minutes = row[8] if len(row) > 8 else None
            deploy_end = row[9] if len(row) > 9 else None
            try:
                eligible_hub_ids = tuple(
                    s.hub_id for s in fleet.hub_capabilities(bank_id) if s.health == "online"
                )
            except LookupError:
                eligible_hub_ids = ()
            # WP-D: the S5.2 PQ filter for PQ-sensitive profiles (DATA_CENTER) runs inside the allocator
            # cycle (`allocator.pq_eligibility.apply_eligibility`, fed by the engine's cycle extras).
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
                        max((to_utc(deploy_end) - to_utc(interval_start)).total_seconds(), 0.0) / 3600.0
                        if deploy_end is not None and as_deployed
                        else None
                    ),
                    market_ref=markets.get(str(obligation_id), FREE),
                    grandfathered=(str(obligation_id), str(bank_id)) in grandfathered,
                )
            )
        return LedgerView(calls=tuple(calls))

    async def _grandfathered(self) -> set[tuple[str, str]]:
        """D-37/K13: `(obligation_id, bank_id)` pairs grandfathered on an unavailable bank
        (`market.availability.GRANDFATHERED_SQL`, the one rule). Unreadable: none (fail closed -- the
        allocator then keeps them off the bank and reports the K13 shortfall; the guardian agrees)."""
        try:
            async with self._pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(GRANDFATHERED_SQL)
                rows = await cur.fetchall()
        except Exception:
            if not self._grandfather_warned:
                logger.error("grandfathered obligations unreadable; none grandfathered", exc_info=True)
                self._grandfather_warned = True
            return set()
        return {(str(o), str(b)) for o, b in rows}

    async def _markets(self, obligation_ids: Sequence[str]) -> dict[str, MarketRef | None]:
        """K15: each obligation's market from its contract (`og.contract.market`/`utility_id`, migration
        0025). Inconsistent market data is `None` (never served while territory is enforced). Unreadable
        (0025 not applied yet) means every contract is FREE, the column's default -- a regulated obligation
        then sits on territory banks FREE may not use, so it fails closed there."""
        if not obligation_ids:
            return {}
        try:
            async with self._pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(_CONTRACT_MARKETS_SQL, {"obligation_ids": list(set(obligation_ids))})
                rows = await cur.fetchall()
        except Exception:
            if not self._market_warned:
                logger.warning("contract markets unreadable; obligations treated as FREE", exc_info=True)
                self._market_warned = True
            return {}
        markets: dict[str, MarketRef | None] = {}
        for obligation_id, market, utility_id, service_type in rows:
            try:
                markets[str(obligation_id)] = market_of(
                    market=market, utility_id=utility_id, service_type=service_type
                )
            except MarketModelError:
                markets[str(obligation_id)] = None
        return markets

    async def ledger_version(self) -> int:
        return await ledger.ledger_version()

    async def persist_grants(self, cycle_id: str, grants: Sequence[ProposedGrant]) -> None:
        self._granted_kw = {}
        for g in grants:
            if g.obligation_id is not None and not g.is_headroom:
                self._granted_kw[g.obligation_id] = self._granted_kw.get(g.obligation_id, 0.0) + g.granted_kw
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

    async def record_shortfalls(
        self, cycle_id: str, shortfalls: Sequence[ShortfallReport], *, now: datetime | None = None
    ) -> None:
        """Keep this cycle's shortfalls for the escalation step, and trace each K13-exception shortfall
        (`R-COMMIT-LOCK-OVERRIDE-L0/L1/L2`, `R-COMMIT-LOCK-INFEASIBLE`) when it starts and again in every
        new 15-min interval it spans: a delivery dip below the committed kW must carry its exception in
        the trace (K13), not only in memory. Not once per 2 s cycle."""
        self.last_shortfalls = [(s.obligation_id, s.reason_code) for s in shortfalls]
        at = now or datetime.now(UTC)
        interval = floor_to_interval(at, 15).isoformat()
        current: set[tuple[str, str, str]] = set()
        for s in shortfalls:
            if s.reason_code not in _K13_SHORTFALL_REASONS:
                continue
            key = (s.obligation_id, s.reason_code, interval)
            current.add(key)
            if key in self._traced_shortfalls or self._trace is None:
                continue
            await self._trace.append(
                f"shortfall-{s.obligation_id}",
                "SHORTFALL",
                "ALLOCATOR_SHORTFALL",
                {
                    "cycle_id": cycle_id,
                    "obligation_id": s.obligation_id,
                    "bank_id": s.bank_id,
                    "shortfall_kw": s.shortfall_kw,
                    "interval_start": interval,
                },
                reason_codes=[s.reason_code],
            )
        self._traced_shortfalls = current
        # K13 audit: each lock exception is a commitment-lock event row (once per episode and interval).
        for obligation_id, reason, _interval in sorted(current):
            await self._record_lock_event(obligation_id, lock_reason_of(reason), at)
        short_now = {s.obligation_id for s in shortfalls if s.reason_code in _K13_SHORTFALL_REASONS}
        await self._restore(cycle_id, short_now, at)
        self._short_prev = short_now

    async def _restore(self, cycle_id: str, short_now: set[str], at: datetime) -> None:
        """Owner decision 2026-09-26 (K13 best effort): once the constraint clears, a short obligation is
        granted its full committed kW again (the allocator re-derives it every cycle). The first cycle it is
        served in full is recorded: trace R-SHORTFALL-RESTORED, a commitment-lock event row, and AT_RISK
        cleared. Obligations already in SHORTFALL (e.g. escalated before a restart) count too, once."""
        self._restored -= short_now
        candidates = (self._short_prev | self._in_shortfall) - short_now - self._restored
        for obligation_id in sorted(candidates):
            committed = self._committed_kw.get(obligation_id)
            granted = self._granted_kw.get(obligation_id, 0.0)
            if committed is None or committed <= 0 or granted < committed - 1e-6:
                continue
            self._restored.add(obligation_id)
            if self._trace is not None:
                try:
                    await self._trace.append(
                        f"shortfall-{obligation_id}",
                        "SHORTFALL",
                        "SHORTFALL_RESTORED",
                        {"cycle_id": cycle_id, "obligation_id": obligation_id, "granted_kw": granted},
                        reason_codes=[R_SHORTFALL_RESTORED],
                    )
                except Exception:
                    logger.exception(
                        "could not trace a restored commitment", extra={"obligation_id": obligation_id}
                    )
            await self._record_lock_event(obligation_id, R_SHORTFALL_RESTORED, at)
            try:
                await contracts.set_obligation_at_risk(
                    UUID(obligation_id),
                    False,
                    reason_code=R_SHORTFALL_RESTORED,
                    payload={"cause": "restored"},
                )
            except Exception:
                logger.exception("could not clear AT_RISK on restore", extra={"obligation_id": obligation_id})

    async def _record_lock_event(self, obligation_id: str, reason_code: str, at: datetime) -> None:
        """02a S2.2 / K13: write the commitment-lock event as an `og.commitment` row carrying the reason and
        superseding the obligation's latest row for the current interval. The committed kW is copied
        unchanged (K13: the lock never reduces the commitment; delivery is metered and settled). Once per
        (obligation, reason, 15-min interval). Best effort: the trace is the safety record."""
        interval = floor_to_interval(at, 15).isoformat()
        if self._lock_events.get((obligation_id, reason_code)) == interval:
            return
        self._lock_events[(obligation_id, reason_code)] = interval
        try:
            async with self._pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    _LOCK_EVENT_SQL, {"obligation_id": obligation_id, "reason_code": reason_code, "now": at}
                )
                commit = getattr(conn, "commit", None)
                if commit is not None:
                    await commit()
        except Exception:
            logger.exception(
                "could not write commitment-lock event",
                extra={"obligation_id": obligation_id, "reason": reason_code},
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
            await self._record_lock_event(event.obligation_id, R_SUBSTITUTION, datetime.now(UTC))

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
        self._alerts_swept = False
        self.as_hold_ids: set[str] = set()
        self._as_ids: set[str] = set()

    async def run(self, now: datetime) -> list[EnergySufficiencyResult]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _ENERGY_SUFFICIENCY_ROWS_SQL, {"now": now, "lookahead_end": now + self._lookahead}
            )
            rows = await cur.fetchall()

        # (obligation_id, average kW over the remaining draw, draw end, customer_id) per bank.
        by_bank: dict[str, list[tuple[str, float, datetime, str | None]]] = {}
        as_holds: set[str] = set()
        as_all: set[str] = set()
        for row in rows:
            obligation_id, bank_id, required_kwh, draw_end, customer_id = row[:5]
            service_type, hold_kw, duration_minutes, as_deployed, deploy_end = (
                *row[5:10],
                None,
                None,
                None,
                False,
                None,
            )[:5]
            if service_type in HOLD_SERVICE_TYPES:
                # Energy hold (ERCOT_AS; D-29 utility toll, 90 min product rule) (Frank #6, NPRR1282): an AS award must keep enough energy above the reserve
                # floor for a FULL deployment of its committed kW -- held or deployed -- not just the kWh
                # of its remaining window.
                kw, draw_end = as_energy_hold(
                    now, hold_kw, duration_minutes, deploy_end if as_deployed else None
                )
                as_all.add(str(obligation_id))
                if not as_deployed:
                    as_holds.add(str(obligation_id))
            else:
                remaining_h = max((draw_end - now).total_seconds(), 0.0) / 3600.0
                kw = float(required_kwh) / remaining_h if remaining_h > 0 else 0.0
            by_bank.setdefault(bank_id, []).append(
                (str(obligation_id), kw, draw_end, str(customer_id) if customer_id else None)
            )
        #: Undeployed AS holds this cycle: energy-short is AT_RISK for them, never a SHORTFALL escalation
        #: (nothing is being delivered short -- the hold itself is the service).
        self.as_hold_ids = as_holds
        self._as_ids = as_all

        if not self._alerts_swept:
            # Reconcile with alerts a previous engine raised (review #14): an obligation whose alert is
            # still open counts as already AT_RISK, so it is not raised twice (the open alert and its ack
            # state are kept); one no longer at risk is cleared below like any recovery.
            await self._adopt_open_alerts()
        results = await self._evaluate_banks(by_bank, now)
        now_at_risk = {r.obligation_id for r in results if r.at_risk}
        recovered = self._at_risk - now_at_risk
        for obligation_id in recovered:
            await self._set_at_risk(obligation_id, False)
        self._at_risk &= now_at_risk  # recovered obligations may alert again on a later entry
        if recovered or not self._alerts_swept:
            # This hook raised ALR-ENERGY-SHORTFALL-RISK, so it clears it (health only auto-clears its own
            # rules).
            await self._clear_resolved_alerts(now_at_risk)
            self._alerts_swept = True
        return results

    async def _adopt_open_alerts(self) -> None:
        try:
            details = await open_alert_details(self._pool, ALR_ENERGY_SHORTFALL_RISK)
        except Exception:
            logger.exception("could not read open energy-shortfall alerts")
            return
        self._at_risk |= {str(d["obligation_id"]) for d in details if d.get("obligation_id")}

    async def _clear_resolved_alerts(self, still_at_risk: set[str]) -> None:
        try:
            await clear_open_alerts(
                self._pool,
                ALR_ENERGY_SHORTFALL_RISK,
                lambda detail: str(detail.get("obligation_id")) not in still_at_risk,
            )
        except Exception:
            logger.exception("could not clear resolved energy-shortfall alerts")

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

            # AS energy hold margin, matching the guardian's G-01-ENERGY floor: the hold keeps reserve +
            # 1% of capacity + kW x duration / eta_d, else the last leases of a full deployment are vetoed.
            # Only hubs with a live SoC hold energy, so only they carry the margin (K1).
            hold_margin_kwh = deliverable_margin_kwh(
                (getattr(h, "e_kwh", None), h.eta_d) for h in online_hubs if h.soc_kwh is not None
            )

            for obligation_id, committed_kw, window_end, customer_id in obligations:
                remaining_window_h = max((window_end - now).total_seconds(), 0.0) / 3600.0
                if obligation_id in self._as_ids and remaining_window_h > 0:
                    committed_kw += hold_margin_kwh / remaining_window_h

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
                if result.at_risk and result.obligation_id not in self._at_risk:
                    self._at_risk.add(result.obligation_id)
                    await self._record_at_risk(result, customer_id)
        await self._record_statuses(results)
        return results

    async def _record_statuses(self, results: list[EnergySufficiencyResult]) -> None:
        """Persists EVERY cycle's results (not just AT_RISK ones) to `og.obligation_energy_status`, so
        `GET /og/api/dispatch/opportunities`/the dispatch SSE stream can show a live
        `energy_margin_kwh`/`time_to_depletion_h` for an obligation that is currently fine (merge task
        item 5). Display state, recomputed every cycle: one transaction per cycle with an asynchronous
        commit, never one synchronous commit per obligation inside the dispatch tick (A11). Best-effort: a
        failure never blocks the AT_RISK trace/alert path, which is the safety-relevant one."""
        if not results:
            return
        try:
            async with self._pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(_ASYNC_COMMIT_SQL)
                for result in results:
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
            logger.exception("failed to persist obligation_energy_status", extra={"count": len(results)})

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
    pool: AsyncConnectionPool,
    trace: TraceStore | None = None,
    *,
    market_model: MarketModel | None = None,
    stored_energy_value: StoredEnergyValueReader | None = None,
    wear_usd_per_kwh: float = DEFAULT_WEAR_USD_PER_KWH,
    m1_by_zone: Mapping[str, float] | None = None,
    hold_floor_in_process: Callable[[str, datetime], float | None] | None = None,
    hold_floor_db: HoldFloorReader | None = None,
) -> tuple[EngineFleetGateway, EngineLedgerGateway, EngineScadaGateway, EngineScheduleGateway]:
    """Convenience constructor for `opengrid.engine.main`: one of each gateway, built once per process
    (the fleet/scada gateways hold no state of their own; the ledger/schedule gateways hold the shared
    pool, and the ledger gateway the process's trace store for substitution events)."""
    return (
        EngineFleetGateway(market_model),
        EngineLedgerGateway(pool, trace),
        EngineScadaGateway(),
        EngineScheduleGateway(
            pool,
            stored_energy_value=stored_energy_value,
            wear_usd_per_kwh=wear_usd_per_kwh,
            m1_by_zone=m1_by_zone,
            hold_floor_in_process=hold_floor_in_process or (lambda bank_id, at: None),
            hold_floor_db=hold_floor_db,
        ),
    )
