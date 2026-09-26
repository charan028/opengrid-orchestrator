"""Read models + the handful of direct writes `api` itself owns (customers/contracts/opportunities
CRUD, retention policy, alert ack) -- 02b S7.1, BUILD.md api row.

`api` never re-derives engine/ledger/guardian logic: `Store` only does plain reads of tables those
modules own, plus the admission call into `opengrid.contracts.admit` for opportunity creation. Every
row shape returned matches `opengrid.core.models` exactly so a router can return the pydantic model
straight from a dict row (psycopg 3 already converts `uuid`/`numeric`/`timestamptz`/`jsonb` to the
matching Python type).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from opengrid.core.models.engine import (
    CommandBatchRow,
    Commitment,
    Grant,
    InvoiceLine,
    Obligation,
    Opportunity,
    Performance,
    Plan,
    Reservation,
    TraceRow,
    Verdict,
)
from opengrid.core.models.platform import Alert, FeedObs


@dataclass(frozen=True, slots=True)
class ForecastPoint:
    series_key: str
    kind: str
    interval_start_utc: datetime
    horizon_step: int
    p10: float
    p50: float
    p90: float
    firm_fitness: str


@dataclass(frozen=True, slots=True)
class BankAggregate:
    bank_id: str
    zone: str
    kva_rating: float
    reserve_kva: float
    feeder_id: str | None
    member_hub_count: int
    load_kva_estimate: float


@dataclass(frozen=True, slots=True)
class ProcessHeartbeat:
    process: str
    pid: int
    ts: datetime
    status: str


@dataclass(frozen=True, slots=True)
class FeedStatusRow:
    source: str
    product: str
    last_value_at: datetime | None
    last_success_at: datetime | None
    consecutive_failures: int
    breaker_open: bool
    active_key: str | None


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    processes: list[ProcessHeartbeat]
    feeds: list[FeedStatusRow]
    hub_health_counts: dict[str, int]
    open_alerts: list[Alert]


@dataclass(frozen=True, slots=True)
class AlertQuery:
    """Filters for `GET /og/api/alerts` (all optional; `open_only` defaults to open alerts)."""

    open_only: bool = True
    severity: str | None = None
    rule: str | None = None
    scope_kind: str | None = None
    scope_ref: str | None = None

    def where(self) -> tuple[str, tuple[Any, ...]]:
        """A parameterised `WHERE` clause (fixed fragments only; values always bound, never inlined)."""
        clauses: list[str] = []
        params: list[Any] = []
        if self.open_only:
            clauses.append("cleared_at IS NULL")
        for column, value in (
            ("severity", self.severity),
            ("rule", self.rule),
            ("scope_kind", self.scope_kind),
            ("scope_ref", self.scope_ref),
        ):
            if value is not None:
                clauses.append(f"{column} = %s")
                params.append(value)
        return ("WHERE " + " AND ".join(clauses) if clauses else ""), tuple(params)


class StoreProtocol(Protocol):
    """The interface routers depend on -- `PgStore` in production, a fake in unit tests."""

    async def list_hubs(
        self, *, zone: str | None, bank_id: str | None, health: str | None, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        """Flat `hub_id`/`bank_id`/`zone`/`soc_kwh`/`p_kw`/`health`/... rows -- `opengrid.ui`'s Fleet
        table needs `bank_id`/`zone` (on `Hub`) alongside `HubState`'s fields, so this is a plain
        `hub JOIN hub_state` projection rather than a single core model."""
        ...

    async def get_hub(self, hub_id: str) -> dict[str, Any] | None:
        """Same flat shape as `list_hubs`'s rows, for the hub drill-down partial."""
        ...

    async def get_bank(self, bank_id: str) -> BankAggregate | None: ...

    async def hub_telemetry_sparkline(self, hub_id: str, *, minutes: int) -> list[dict[str, Any]]: ...

    async def feed_series(
        self, *, product: str | None, series: str | None, t0: datetime, t1: datetime
    ) -> list[FeedObs]: ...

    async def feed_statuses(self) -> list[FeedStatusRow]: ...

    async def forecast_series(self, *, series_key: str, kind: str) -> list[ForecastPoint]: ...

    async def list_opportunities(self, *, state: str | None) -> list[Opportunity]: ...

    async def list_obligations(self, *, state: str | None) -> list[Obligation]:
        """The dispatch Kanban's real pipeline rows (02b S8 screen 3: "offered -> committed ->
        delivering -> fulfilled"). `Opportunity.state` only ever reaches `OFFERED/SELECTED/REJECTED/
        EXPIRED` (02a S1.4) -- the committed/delivering/fulfilled stages the screen shows are
        `Obligation.state` (02a S1.5), which is what `opengrid.ui.routes.dispatch.pipeline_view`
        (`committed_qty_kw`, `tier`, `at_risk`, `last_reason_code`) already parses."""
        ...

    async def count_active_commitments(self) -> int:
        """`og.obligation` rows in `COMMITTED`/`DELIVERING` -- the control room's "active commitment
        count" (02b S8 screen 1). Distinct from `list_opportunities`: `Opportunity.state` has no
        `COMMITTED` value (that's an `Obligation` state, 02a S1.5)."""
        ...

    async def hub_health_summary(self) -> dict[str, int]:
        """`{"total", "online", "stale", "offline"}` counts over the whole registered fleet (`og.hub`),
        not a `limit`/`offset` page of it -- `GET /og/api/fleet/hubs` caps at 200 rows/page
        (`routers/fleet.py::list_hubs`'s `limit: int = Query(default=200, le=2000)`), so a smoke test
        or dashboard counting "how many of the ~2,000 hubs are online" by paging `/hubs` either
        under-counts at the default page size or has to page through all of them; this is the O(1)
        query for that count instead (task item A2). `og.hub_state.health` only ever records
        `online`/`stale`/`fault` for a hub that has reported telemetry at least once
        (`opengrid.core.models.platform.HubState.health`); a hub with no `hub_state` row yet is folded
        into `offline` here alongside `fault`, since neither has ever demonstrated it is reachable."""
        ...

    async def latest_plan(self) -> Plan | None: ...

    async def ledger_timeline(self, bank_id: str) -> list[Reservation]: ...

    async def list_grants(self, bank_id: str) -> list[Grant]: ...

    async def list_commitments(self, *, limit: int = 200) -> list[Commitment]:
        """Recent commitments across every bank -- `og.commitment` has no `bank_id` column (it is keyed
        by `obligation_id`/interval, 02a S1.6), so this is not bank-scoped like `ledger_timeline`; the
        dispatch screen's commitment-lock event feed filters by reason code/`supersedes` itself."""
        ...

    async def profitability_summary(
        self, *, service: str | None, day: date | None
    ) -> list[dict[str, Any]]: ...

    async def invoice_lines(self, *, t0: date, t1: date) -> list[InvoiceLine]: ...

    async def performance_summary(self, *, t0: date, t1: date) -> list[Performance]: ...

    async def trace_events(
        self, *, event_class: str | None, t0: datetime | None, t1: datetime | None, limit: int
    ) -> list[TraceRow]: ...

    async def list_stream_ids(self) -> list[str]:
        """Every distinct `stream_id` ever written -- used only to enumerate what `POST
        /og/api/trace/verify` should check when no single `stream_id` is given; not the hash-chain
        `TraceBackend.stream_ids()` (that stays private to `opengrid.trace`), just a plain read of the
        column for this one purpose."""
        ...

    async def health_snapshot(self, *, heartbeat_miss_threshold_s: float) -> HealthSnapshot: ...

    async def list_alerts(self, *, open_only: bool) -> list[Alert]: ...

    async def ack_alert(self, alert_id: int, *, operator: str) -> Alert | None: ...

    async def list_charge_windows(self) -> list[dict[str, Any]]:
        """Every `og.owner_charge_window` row (D-30): `scope_kind, scope_ref, windows, updated_by,
        updated_at`, least specific scope first."""
        ...

    async def set_charge_window(
        self, scope_kind: str, scope_ref: str, windows: list[str], *, updated_by: str
    ) -> None:
        """Insert or replace one scope's windows."""
        ...

    async def delete_charge_window(self, scope_kind: str, scope_ref: str) -> bool:
        """Remove one scope's override; False if there was none."""
        ...

    async def charge_window_topology(
        self, *, hub_id: str | None, bank_id: str | None
    ) -> dict[str, Any] | None:
        """`{hub_id, bank_id, zone, feeder_id, substation_id}` of a hub or a bank (None if unknown), for
        `opengrid.core.charge_windows.resolve`."""
        ...

    async def live_manual_targets(self) -> list[dict[str, Any]]:
        """Every hub under a live manual target: `hub_id, p_kw_target, issued_at, expires_at, trace_id,
        proposer, reason` (same rule as `live_manual_target_hubs`)."""
        ...

    async def live_manual_target_hubs(self, trace_id: UUID) -> dict[str, float]:
        """`{hub_id: p_kw_target}` for the hubs whose CURRENT manual target (newest MANUAL_TARGET per hub
        by `issued_at`, not yet expired -- `opengrid.engine.manual`'s rule) is the event `trace_id`."""
        ...

    async def alert_ack_states(self, alert_ids: list[int]) -> dict[int, str | None]:
        """`{id: acked_by}` for the ids that exist (`acked_by` None = not yet acked) -- one read for the
        bulk ack's per-id outcome; missing ids are absent."""
        ...

    async def query_alerts(self, query: AlertQuery, *, limit: int, offset: int) -> tuple[list[Alert], int]:
        """One page of alerts matching `query` (newest first, scope columns included) and the total match
        count."""
        ...

    async def alert_group_counts(self, query: AlertQuery) -> list[dict[str, Any]]:
        """`[{rule, scope_kind, scope_ref, count}]` over the alerts matching `query`, largest first."""
        ...

    async def current_ledger_version(self) -> int:
        """Read-only: the highest `ledger_version` ever written to `og.reservation`, used only to
        stamp a manually-proposed `command_batch` header (`opengrid.ledger` owns the real allocator
        write path; this is a plain read of the same column, not a re-derivation of its logic)."""
        ...

    async def insert_command_batch(self, batch: CommandBatchRow) -> None: ...

    async def get_verdict(self, command_batch_id: UUID) -> Verdict | None: ...

    async def notify(self, channel: str, payload: dict[str, Any]) -> None:
        """`SELECT pg_notify(channel, json)` -- the LISTEN/NOTIFY request intake
        `opengrid.safestop.pg_backend`'s `REQUEST_CHANNEL` documents as `og-api`'s side of the
        PROPOSE/CONFIRM protocol (`opengrid.safestop.main`'s own module docstring)."""
        ...

    async def latest_stop_event(self, scope_kind: str, scope_ref: str) -> tuple[str, datetime] | None:
        """(action, created_at) of the most recent `stop_event` for this scope, or `None`. Read-only;
        `og.stop_event` is written only by `opengrid.safestop`/the guardian's Tier-2 path."""
        ...

    async def retention_policy(self) -> list[dict[str, Any]]: ...

    async def upsert_retention_policy(self, event_class: str, retention_days: int) -> None: ...

    async def insert_operator_action(
        self,
        *,
        operator_ref: str,
        action_kind: str,
        target_ref: str | None,
        tier: str | None,
        reason: str | None,
        trace_id: UUID | None,
        confirmed_at: datetime | None,
        approver_ref: str | None = None,
        created_at: datetime | None = None,
    ) -> UUID:
        """`approver_ref` defaults to `operator_ref` (single-operator actions); the two-person stop
        RELEASE passes the second operator, and `created_at` = when operator A requested it (the
        guardian's `requested_at`; defaults to the insert time)."""
        ...

    async def insert_as_deployment(
        self,
        *,
        obligation_id: UUID | None,
        start_at: datetime,
        end_at: datetime,
        requested_by: str,
        reason: str,
    ) -> UUID:
        """An ERCOT_AS deployment (`og.as_deployment`, source OPERATOR): the held award(s) discharge up to
        their committed kW while it is active. `obligation_id` None deploys every ERCOT_AS award."""
        ...

    async def list_active_as_deployments(self) -> list[dict[str, Any]]: ...

    async def get_as_award(self, obligation_id: UUID) -> dict[str, Any] | None:
        """`{service_type, state, variant, duration_minutes, has_active_deployment}` of an obligation, or
        None if it does not exist -- the AS deployment / utility call route's validation read. `variant` is
        its contract's (e.g. TOLLING); `duration_minutes` is the full-deployment duration of the obligation's
        OWN product rule (its opportunity's `product_rule_id`), None when unknown; `has_active_deployment`
        is true while an active `og.as_deployment` already covers it (its own row, or a fleet-wide row for
        an ERCOT_AS award)."""
        ...

    async def cancel_as_deployment(self, deployment_id: UUID) -> bool:
        """End an active deployment now (sets `cancelled_at`; never a delete). False if none active."""
        ...


#: The live manual target per hub. Mirrors `opengrid.engine.manual.parse_targets` (newest `issued_at` per
#: hub wins, expired ignored) in SQL, because og-api must not import the engine package (it pulls the
#: allocator and guardian modules). `{extra}` is one fixed filter fragment, never caller input.
_LIVE_MANUAL_TARGETS_SQL = """
    SELECT hub_id, p_kw_target, issued_at, expires_at, trace_id, proposer, reason FROM (
        SELECT h.hub_id, t.trace_id, (t.payload ->> 'p_kw_target')::float8 AS p_kw_target,
               COALESCE((t.payload ->> 'issued_at')::timestamptz, t.created_at) AS issued_at,
               (t.payload ->> 'expires_at')::timestamptz AS expires_at,
               t.payload ->> 'proposer' AS proposer, t.payload ->> 'reason' AS reason,
               row_number() OVER (
                   PARTITION BY h.hub_id
                   ORDER BY COALESCE((t.payload ->> 'issued_at')::timestamptz, t.created_at) DESC
               ) AS rn
        FROM og.trace t
        CROSS JOIN LATERAL jsonb_array_elements_text(t.payload -> 'hub_ids') AS h(hub_id)
        WHERE t.event_class = 'MANUAL_TARGET' AND t.created_at > now() - interval '24 hours'
    ) latest
    WHERE rn = 1 AND expires_at > now() {extra}
    ORDER BY hub_id
"""


def _row_or_none(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    return rows[0] if rows else None


class PgStore:
    """psycopg 3 implementation of `StoreProtocol` against the `og` schema."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def _fetch(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return list(await cur.fetchall())

    async def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        """`pool.connection()` commits on clean exit (psycopg's "connection context behaviour"), so no
        explicit commit is needed here."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
            return cur.rowcount

    # -- fleet ------------------------------------------------------------------------------------

    async def fleet_rows(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        """Read-only SELECT/EXPLAIN statements built by `opengrid.api.routers.fleet_search` (the Fleet
        table at scale: keyset pages, typeahead, select-all-matching, hub detail). Every statement is
        assembled there from hard-coded fragments with values bound as `%s` parameters."""
        return await self._fetch(sql, params)

    async def list_hubs(
        self, *, zone: str | None, bank_id: str | None, health: str | None, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        clauses: list[str] = ["1=1"]
        params: list[Any] = []
        if zone is not None:
            clauses.append("h.zone = %s")
            params.append(zone)
        if bank_id is not None:
            clauses.append("h.bank_id = %s")
            params.append(bank_id)
        if health is not None:
            clauses.append("s.health = %s")
            params.append(health)
        # `clauses` is built only from this method's own hard-coded column=%s fragments (never from
        # caller-supplied strings); every value is still bound as a `%s` parameter below, so this is
        # dynamic clause selection, not string-built SQL values (the same pattern repeats in every
        # other list_*/*_events method below, each with its own lint suppression on the literal).
        sql = f"""
            SELECT h.hub_id, h.bank_id, h.zone, s.soc_kwh, s.p_kw, s.health, s.lease_epoch,
                   s.lease_expires_at, s.last_command_id, s.last_seen_at, s.fault_code
            FROM og.hub_state s JOIN og.hub h ON h.hub_id = s.hub_id
            WHERE {" AND ".join(clauses)}
            ORDER BY s.hub_id LIMIT %s OFFSET %s
        """  # noqa: S608
        return await self._fetch(sql, (*params, limit, offset))

    async def get_hub(self, hub_id: str) -> dict[str, Any] | None:
        rows = await self._fetch(
            """
            SELECT h.hub_id, h.bank_id, h.zone, h.e_kwh, h.r_kwh, h.p_kw AS rated_p_kw, h.eta_c, h.eta_d,
                   h.lat, h.lon, s.soc_kwh, s.p_kw, s.health, s.lease_epoch, s.lease_expires_at,
                   s.last_command_id, s.last_seen_at, s.fault_code,
                   -- hub detail drawer (migrations 0032/0036): asset dates and device-reported identity
                   h.units, h.installed_at, h.last_serviced_at, h.serial_number, h.manufacturer, h.model,
                   h.firmware_version, h.hardware_revision, h.commissioned_at, h.inverter_model,
                   h.device_info_at
            FROM og.hub h JOIN og.hub_state s ON s.hub_id = h.hub_id WHERE h.hub_id = %s
            """,
            (hub_id,),
        )
        return _row_or_none(rows)

    async def hub_telemetry_sparkline(self, hub_id: str, *, minutes: int) -> list[dict[str, Any]]:
        return await self._fetch(
            """
            SELECT ts, soc_kwh, p_kw FROM og.telemetry
            WHERE hub_id = %s AND ts >= now() - (%s || ' minutes')::interval
            ORDER BY ts ASC
            """,
            (hub_id, minutes),
        )

    async def get_bank(self, bank_id: str) -> BankAggregate | None:
        rows = await self._fetch(
            """
            SELECT b.bank_id, b.zone, b.kva_rating, b.reserve_kva, b.feeder_id,
                   count(h.hub_id) AS member_hub_count,
                   coalesce(sum(abs(s.p_kw)), 0) AS load_kva_estimate
            FROM og.bank b
            LEFT JOIN og.hub h ON h.bank_id = b.bank_id
            LEFT JOIN og.hub_state s ON s.hub_id = h.hub_id
            WHERE b.bank_id = %s
            GROUP BY b.bank_id, b.zone, b.kva_rating, b.reserve_kva, b.feeder_id
            """,
            (bank_id,),
        )
        row = _row_or_none(rows)
        return BankAggregate(**row) if row is not None else None

    # -- markets / forecast -------------------------------------------------------------------------

    async def feed_series(
        self, *, product: str | None, series: str | None, t0: datetime, t1: datetime
    ) -> list[FeedObs]:
        clauses: list[str] = ["ts BETWEEN %s AND %s"]
        params: list[Any] = [t0, t1]
        if product is not None:
            clauses.append("product = %s")
            params.append(product)
        if series is not None:
            clauses.append("series = %s")
            params.append(series)
        sql = f"""
            SELECT source, product, series, ts, value, unit, quality, recorded_at
            FROM og.feed_obs WHERE {" AND ".join(clauses)} ORDER BY ts ASC
        """  # noqa: S608 -- clause fragments are hard-coded, values are bound as `%s` params
        rows = await self._fetch(sql, tuple(params))
        return [FeedObs(**row) for row in rows]

    async def feed_statuses(self) -> list[FeedStatusRow]:
        rows = await self._fetch(
            """
            SELECT source, product, last_value_at, last_success_at, consecutive_failures,
                   breaker_open, active_key
            FROM og.feed_status ORDER BY source, product
            """
        )
        return [FeedStatusRow(**row) for row in rows]

    async def forecast_series(self, *, series_key: str, kind: str) -> list[ForecastPoint]:
        rows = await self._fetch(
            """
            SELECT series_key, kind, interval_start_utc, horizon_step, p10, p50, p90, firm_fitness
            FROM og.forecast WHERE series_key = %s AND kind = %s ORDER BY horizon_step ASC
            """,
            (series_key, kind),
        )
        return [ForecastPoint(**row) for row in rows]

    # -- dispatch / ledger --------------------------------------------------------------------------

    async def list_opportunities(self, *, state: str | None) -> list[Opportunity]:
        clauses: list[str] = ["1=1"]
        params: list[Any] = []
        if state is not None:
            clauses.append("state = %s")
            params.append(state)
        sql = f"""
            SELECT opportunity_id, contract_id, product_rule_id, window_start, window_end, requested_kw,
                   value_per_mwh, scenario_basis, state, reason_code, admitted_at, decided_at, gate_id
            FROM og.opportunity WHERE {" AND ".join(clauses)} ORDER BY admitted_at DESC LIMIT 500
        """  # noqa: S608 -- clause fragments are hard-coded, values are bound as `%s` params
        rows = await self._fetch(sql, tuple(params))
        return [Opportunity(**row) for row in rows]

    async def list_obligations(self, *, state: str | None) -> list[Obligation]:
        clauses: list[str] = ["1=1"]
        params: list[Any] = []
        if state is not None:
            clauses.append("o.state = %s")
            params.append(state)
        sql = f"""
            SELECT o.obligation_id, o.opportunity_id, o.contract_id, o.service_type, o.tier,
                   o.window_start, o.window_end, o.committed_qty_kw, o.state, o.at_risk,
                   o.last_reason_code, o.version,
                   es.margin_kwh AS energy_margin_kwh, es.time_to_depletion_h
            FROM og.obligation o
            LEFT JOIN og.obligation_energy_status es ON es.obligation_id = o.obligation_id
            WHERE {" AND ".join(clauses)} ORDER BY o.updated_at DESC LIMIT 500
        """  # noqa: S608 -- clause fragments are hard-coded, values are bound as `%s` params
        rows = await self._fetch(sql, tuple(params))
        return [Obligation(**row) for row in rows]

    async def count_active_commitments(self) -> int:
        rows = await self._fetch(
            "SELECT count(*) AS n FROM og.obligation WHERE state IN ('COMMITTED', 'DELIVERING')"
        )
        return int(rows[0]["n"]) if rows else 0

    async def hub_health_summary(self) -> dict[str, int]:
        total_rows = await self._fetch("SELECT count(*) AS n FROM og.hub")
        total = int(total_rows[0]["n"]) if total_rows else 0
        health_rows = await self._fetch("SELECT health, count(*) AS n FROM og.hub_state GROUP BY health")
        by_health = {row["health"]: int(row["n"]) for row in health_rows}
        online = by_health.get("online", 0)
        stale = by_health.get("stale", 0)
        # "fault" and "never reported" both fold into "offline" (StoreProtocol.hub_health_summary docstring).
        offline = max(total - online - stale, 0)
        return {"total": total, "online": online, "stale": stale, "offline": offline}

    async def latest_plan(self) -> Plan | None:
        rows = await self._fetch(
            """
            SELECT plan_id, plan_mode, gate_kind, horizon_start, horizon_end, scenario_set,
                   solver_status, solver_gap, solver_time_ms, objective_value, superseded_by
            FROM og.plan ORDER BY created_at DESC LIMIT 1
            """
        )
        row = _row_or_none(rows)
        return Plan(**row) if row is not None else None

    async def ledger_timeline(self, bank_id: str) -> list[Reservation]:
        rows = await self._fetch_by_uuid_bank_id(
            """
            SELECT reservation_id, obligation_id, bank_id, kind, amount, interval_start, interval_end,
                   ledger_version, released_at, release_reason
            FROM og.reservation WHERE bank_id = %s ORDER BY interval_start ASC LIMIT 1000
            """,
            bank_id,
        )
        return [Reservation(**row) for row in rows]

    async def list_grants(self, bank_id: str) -> list[Grant]:
        rows = await self._fetch_by_uuid_bank_id(
            """
            SELECT grant_id, cycle_id, obligation_id, bank_id, granted_kw, is_headroom, ledger_version,
                   command_batch_id
            FROM og.grant WHERE bank_id = %s ORDER BY created_at DESC LIMIT 200
            """,
            bank_id,
        )
        return [Grant(**row) for row in rows]

    async def _fetch_by_uuid_bank_id(self, sql: str, bank_id: str) -> list[dict[str, Any]]:
        """`og.reservation`/`og.grant.bank_id` are TEXT since `migrations/0004_bank_id_text.sql`, matching
        the fleet twin's `"bank-000"` codes. The earlier "not a UUID -> return []" short-circuit outlived
        that migration and hid every reservation and grant from the Dispatch ledger timeline (seen live
        2026-09-26: 22 rows in og.reservation for bank-007, `/og/api/ledger/bank-007/timeline` returned
        none)."""
        return await self._fetch(sql, (bank_id,))

    async def list_commitments(self, *, limit: int = 200) -> list[Commitment]:
        rows = await self._fetch(
            """
            SELECT commitment_id, obligation_id, plan_id, interval_start, interval_end, committed_kw,
                   variable_kind, supersedes, reason_code
            FROM og.commitment ORDER BY created_at DESC LIMIT %s
            """,
            (limit,),
        )
        return [Commitment(**row) for row in rows]

    # -- profitability / billing ---------------------------------------------------------------------

    async def profitability_summary(self, *, service: str | None, day: date | None) -> list[dict[str, Any]]:
        """Per-obligation-interval P&L rows (not aggregated): `opengrid.ui.routes.profitability`'s
        `profitability_table_view`/`lp_vs_baseline_view` key off `row["obligation_id"]` for the table
        and the LP-vs-baseline chart, so a `GROUP BY service/day` summary would drop the very column
        the screen displays rows by. Summing/averaging for display is the UI's job (it already builds
        column totals itself)."""
        clauses: list[str] = ["1=1"]
        params: list[Any] = []
        if service is not None:
            clauses.append("o.service_type = %s")
            params.append(service)
        if day is not None:
            clauses.append("p.interval_start::date = %s")
            params.append(day)
        sql = f"""
            SELECT p.obligation_id, o.service_type, o.contract_id, p.interval_start, p.interval_end,
                   p.revenue, p.energy_cost, p.degradation_cost, p.penalty, p.net_value,
                   p.rule_baseline_value, p.forgone_upside, p.delivery_charge
            FROM og.pnl p JOIN og.obligation o ON o.obligation_id = p.obligation_id
            WHERE {" AND ".join(clauses)}
            ORDER BY p.interval_start DESC LIMIT 1000
        """  # noqa: S608 -- clause fragments are hard-coded, values are bound as `%s` params
        return await self._fetch(sql, tuple(params))

    async def invoice_lines(self, *, t0: date, t1: date) -> list[InvoiceLine]:
        rows = await self._fetch(
            """
            SELECT invoice_line_id, contract_id, obligation_id, period_start, period_end, line_type,
                   quantity, unit, rate, amount, status, supersedes, trace_roll_up, version
            FROM og.invoice_line WHERE period_start >= %s AND period_end <= %s
            ORDER BY period_start ASC, contract_id
            """,
            (t0, t1),
        )
        return [InvoiceLine(**row) for row in rows]

    async def performance_summary(self, *, t0: date, t1: date) -> list[Performance]:
        rows = await self._fetch(
            """
            SELECT performance_id, obligation_id, interval_start, interval_end, compliance_pct,
                   season_pct, availability_pct, response_time_s, passed_threshold
            FROM og.performance WHERE interval_start::date >= %s AND interval_end::date <= %s
            ORDER BY interval_start ASC, obligation_id
            """,
            (t0, t1),
        )
        return [Performance(**row) for row in rows]

    # -- trace ----------------------------------------------------------------------------------------

    async def trace_events(
        self, *, event_class: str | None, t0: datetime | None, t1: datetime | None, limit: int
    ) -> list[TraceRow]:
        clauses: list[str] = ["1=1"]
        params: list[Any] = []
        if event_class is not None:
            clauses.append("event_class = %s")
            params.append(event_class)
        if t0 is not None:
            clauses.append("created_at >= %s")
            params.append(t0)
        if t1 is not None:
            clauses.append("created_at <= %s")
            params.append(t1)
        sql = f"""
            SELECT trace_id, parent_trace_id, decision_type, event_class, stream_id, seq, scope,
                   payload, reason_codes, prev_hash, hash
            FROM og.trace WHERE {" AND ".join(clauses)} ORDER BY created_at DESC LIMIT %s
        """  # noqa: S608 -- clause fragments are hard-coded, values are bound as `%s` params
        rows = await self._fetch(sql, (*params, limit))
        return [TraceRow(**row) for row in rows]

    async def list_stream_ids(self) -> list[str]:
        rows = await self._fetch("SELECT DISTINCT stream_id FROM og.trace")
        return [row["stream_id"] for row in rows]

    # -- health -----------------------------------------------------------------------------------

    async def health_snapshot(self, *, heartbeat_miss_threshold_s: float) -> HealthSnapshot:
        hb_rows = await self._fetch("SELECT process, pid, ts, status FROM og.heartbeat ORDER BY process")
        feeds = await self.feed_statuses()
        hub_counts_rows = await self._fetch("SELECT health, count(*) AS n FROM og.hub_state GROUP BY health")
        alerts = await self.list_alerts(open_only=True)
        return HealthSnapshot(
            processes=[ProcessHeartbeat(**row) for row in hb_rows],
            feeds=feeds,
            hub_health_counts={row["health"]: row["n"] for row in hub_counts_rows},
            open_alerts=alerts,
        )

    async def list_alerts(self, *, open_only: bool) -> list[Alert]:
        clause = "WHERE cleared_at IS NULL" if open_only else ""
        rows = await self._fetch(
            f"""
            SELECT id, rule, severity, summary, detail, opened_at, cleared_at, acked_by
            FROM og.alert {clause} ORDER BY opened_at DESC LIMIT 500
            """  # noqa: S608 -- `clause` is one of two hard-coded literals, never caller input
        )
        return [Alert(**row) for row in rows]

    async def ack_alert(self, alert_id: int, *, operator: str) -> Alert | None:
        rows = await self._fetch(
            "UPDATE og.alert SET acked_by = %s WHERE id = %s RETURNING "
            "id, rule, severity, summary, detail, opened_at, cleared_at, acked_by",
            (operator, alert_id),
        )
        row = _row_or_none(rows)
        return Alert(**row) if row is not None else None

    async def list_charge_windows(self) -> list[dict[str, Any]]:
        return await self._fetch(
            """
            SELECT scope_kind, scope_ref, windows, updated_by, updated_at FROM og.owner_charge_window
            ORDER BY array_position(
                ARRAY['FLEET','PROVIDER','ZONE','SUBSTATION','FEEDER','BANK','HUB'], scope_kind), scope_ref
            """
        )

    async def set_charge_window(
        self, scope_kind: str, scope_ref: str, windows: list[str], *, updated_by: str
    ) -> None:
        await self._execute(
            """
            INSERT INTO og.owner_charge_window (scope_kind, scope_ref, windows, updated_by, updated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (scope_kind, scope_ref) DO UPDATE SET
                windows = EXCLUDED.windows, updated_by = EXCLUDED.updated_by, updated_at = EXCLUDED.updated_at
            """,
            (scope_kind, scope_ref, windows, updated_by),
        )

    async def delete_charge_window(self, scope_kind: str, scope_ref: str) -> bool:
        deleted = await self._execute(
            "DELETE FROM og.owner_charge_window WHERE scope_kind = %s AND scope_ref = %s",
            (scope_kind, scope_ref),
        )
        return deleted > 0

    async def charge_window_topology(
        self, *, hub_id: str | None, bank_id: str | None
    ) -> dict[str, Any] | None:
        rows = await self._fetch(
            """
            SELECT h.hub_id, b.bank_id, b.zone, b.feeder_id,
                   (SELECT a.substation_id FROM og.asset a
                    WHERE a.bank_id = b.bank_id AND a.substation_id IS NOT NULL
                    ORDER BY a.asset_id LIMIT 1) AS substation_id
            FROM og.bank b
            LEFT JOIN og.hub h ON h.bank_id = b.bank_id AND h.hub_id = %s
            WHERE (%s::text IS NOT NULL AND h.hub_id IS NOT NULL) OR (%s::text IS NULL AND b.bank_id = %s)
            LIMIT 1
            """,
            (hub_id, hub_id, hub_id, bank_id),
        )
        return _row_or_none(rows)

    async def live_manual_targets(self) -> list[dict[str, Any]]:
        return await self._fetch(_LIVE_MANUAL_TARGETS_SQL.format(extra=""))

    async def live_manual_target_hubs(self, trace_id: UUID) -> dict[str, float]:
        rows = await self._fetch(_LIVE_MANUAL_TARGETS_SQL.format(extra="AND trace_id = %s"), (trace_id,))
        return {str(r["hub_id"]): float(r["p_kw_target"]) for r in rows}

    async def alert_ack_states(self, alert_ids: list[int]) -> dict[int, str | None]:
        if not alert_ids:
            return {}
        rows = await self._fetch("SELECT id, acked_by FROM og.alert WHERE id = ANY(%s)", (alert_ids,))
        return {int(row["id"]): row["acked_by"] for row in rows}

    async def query_alerts(self, query: AlertQuery, *, limit: int, offset: int) -> tuple[list[Alert], int]:
        where, params = query.where()
        rows = await self._fetch(
            f"""
            SELECT id, rule, severity, summary, detail, opened_at, cleared_at, acked_by, scope_kind, scope_ref
            FROM og.alert {where} ORDER BY opened_at DESC, id DESC LIMIT %s OFFSET %s
            """,  # noqa: S608 -- `where` is built from fixed fragments; every value is a bound parameter
            (*params, limit, offset),
        )
        total = await self._fetch(f"SELECT count(*) AS n FROM og.alert {where}", params)  # noqa: S608
        return [Alert(**row) for row in rows], int(total[0]["n"]) if total else 0

    async def alert_group_counts(self, query: AlertQuery) -> list[dict[str, Any]]:
        where, params = query.where()
        return await self._fetch(
            f"""
            SELECT rule, scope_kind, scope_ref, count(*) AS count
            FROM og.alert {where}
            GROUP BY rule, scope_kind, scope_ref
            ORDER BY count(*) DESC, rule, scope_kind, scope_ref
            """,  # noqa: S608 -- see query_alerts
            params,
        )

    # -- manual command batch header / verdict polling (02b S7.1/S7.3) ------------------------------

    async def current_ledger_version(self) -> int:
        rows = await self._fetch("SELECT COALESCE(MAX(ledger_version), 0) AS v FROM og.reservation")
        return int(rows[0]["v"]) if rows else 0

    async def insert_command_batch(self, batch: CommandBatchRow) -> None:
        await self._execute(
            """
            INSERT INTO og.command_batch
                (command_batch_id, cycle_id, ledger_version, submission_id, command_count,
                 merkle_root, trace_pre_image_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                batch.command_batch_id,
                batch.cycle_id,
                batch.ledger_version,
                batch.submission_id,
                batch.command_count,
                batch.merkle_root,
                batch.trace_pre_image_id,
            ),
        )

    async def get_verdict(self, command_batch_id: UUID) -> Verdict | None:
        rows = await self._fetch(
            """
            SELECT verdict_id, command_batch_id, outcome, vetoed_rule_ids, latency_ms, inputs_hash,
                   signature, signed_at
            FROM og.verdict WHERE command_batch_id = %s
            """,
            (command_batch_id,),
        )
        row = _row_or_none(rows)
        if row is None:
            return None
        row = {**row, "vetoed_rule_ids": row["vetoed_rule_ids"] or []}
        return Verdict(**row)

    # -- safestop notify/poll (02b S7.1/S7.3) --------------------------------------------------------

    async def notify(self, channel: str, payload: dict[str, Any]) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT pg_notify(%s, %s)", (channel, json.dumps(payload)))

    async def latest_stop_event(self, scope_kind: str, scope_ref: str) -> tuple[str, datetime] | None:
        rows = await self._fetch(
            """
            SELECT action, created_at FROM og.stop_event
            WHERE scope_kind = %s AND scope_ref = %s ORDER BY created_at DESC LIMIT 1
            """,
            (scope_kind, scope_ref),
        )
        row = _row_or_none(rows)
        return (row["action"], row["created_at"]) if row is not None else None

    # -- retention ------------------------------------------------------------------------------------

    async def retention_policy(self) -> list[dict[str, Any]]:
        return await self._fetch(
            "SELECT event_class, retention_days, prune_after_checkpoint FROM og.retention_policy "
            "ORDER BY event_class"
        )

    async def upsert_retention_policy(self, event_class: str, retention_days: int) -> None:
        await self._execute(
            """
            INSERT INTO og.retention_policy (event_class, retention_days)
            VALUES (%s, %s)
            ON CONFLICT (event_class) DO UPDATE SET retention_days = EXCLUDED.retention_days,
                                                     updated_at = now()
            """,
            (event_class, retention_days),
        )

    # -- operator_action --------------------------------------------------------------------------

    async def insert_operator_action(
        self,
        *,
        operator_ref: str,
        action_kind: str,
        target_ref: str | None,
        tier: str | None,
        reason: str | None,
        trace_id: UUID | None,
        confirmed_at: datetime | None,
        approver_ref: str | None = None,
        created_at: datetime | None = None,
    ) -> UUID:
        action_id = uuid4()
        await self._execute(
            """
            INSERT INTO og.operator_action
                (operator_action_id, operator_ref, action_kind, target_ref, tier, reason,
                 confirmed_at, approver_ref, trace_id, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, coalesce(%s, now()))
            """,
            (
                action_id,
                operator_ref,
                action_kind,
                target_ref,
                tier,
                reason,
                confirmed_at,
                approver_ref if approver_ref is not None else operator_ref,
                trace_id,
                created_at,
            ),
        )
        return action_id

    # -- ERCOT_AS deployments (og.as_deployment, migration 0020) -----------------------------------

    async def insert_as_deployment(
        self,
        *,
        obligation_id: UUID | None,
        start_at: datetime,
        end_at: datetime,
        requested_by: str,
        reason: str,
    ) -> UUID:
        deployment_id = uuid4()
        await self._execute(
            """
            INSERT INTO og.as_deployment
                (deployment_id, obligation_id, start_at, end_at, source, requested_by, reason)
            VALUES (%s, %s, %s, %s, 'OPERATOR', %s, %s)
            """,
            (deployment_id, obligation_id, start_at, end_at, requested_by, reason),
        )
        return deployment_id

    async def get_as_award(self, obligation_id: UUID) -> dict[str, Any] | None:
        return _row_or_none(
            await self._fetch(
                """
                SELECT o.service_type, o.state, c.variant, pr.duration_minutes,
                       EXISTS (
                           SELECT 1 FROM og.as_deployment d
                           WHERE d.cancelled_at IS NULL AND d.start_at <= now() AND d.end_at > now()
                             AND (d.obligation_id = o.obligation_id
                                  OR (d.obligation_id IS NULL AND o.service_type = 'ERCOT_AS'))
                       ) AS has_active_deployment
                FROM og.obligation o
                JOIN og.contract c ON c.contract_id = o.contract_id
                LEFT JOIN og.opportunity op ON op.opportunity_id = o.opportunity_id
                LEFT JOIN og.product_rule pr ON pr.product_rule_id = op.product_rule_id
                WHERE o.obligation_id = %s
                """,
                (obligation_id,),
            )
        )

    async def list_active_as_deployments(self) -> list[dict[str, Any]]:
        return await self._fetch(
            """
            SELECT deployment_id, obligation_id, start_at, end_at, source, requested_by, reason
            FROM og.as_deployment
            WHERE cancelled_at IS NULL AND end_at > now()
            ORDER BY start_at
            """
        )

    async def cancel_as_deployment(self, deployment_id: UUID) -> bool:
        updated = await self._execute(
            """
            UPDATE og.as_deployment SET cancelled_at = now()
            WHERE deployment_id = %s AND cancelled_at IS NULL AND end_at > now()
            """,
            (deployment_id,),
        )
        return updated > 0


def jsonb(obj: dict[str, Any]) -> Jsonb:
    return Jsonb(obj)
