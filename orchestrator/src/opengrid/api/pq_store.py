"""Read side of the power-quality and asset-health API (07-delivery/06 S6.6/S6.7, WP-J).

Owns only the SQL the `opengrid.api.routers.pq` endpoints need that no other package already exposes:
hub location, raw-capture index listing, bank membership, an obligation's bound PQ envelope, and the
asset-health/calibration/work-order listings. Everything else is delegated, never re-implemented
(BUILD.md S1):

- waveform summaries: `opengrid.pq_ingest.pg_backend.PgPqIngestBackend.latest_summaries` (the read
  `opengrid.pq_ingest.latest_summaries` documents as "the building block WP-J's query API reads");
- a hub's open work order: `opengrid.assets.repo.PgWorkOrderRepo.open_for_hub`.

Also the two read-side helpers the bank-PQ and compliance routes share (`measure_bank`,
`overall_verdict`). Read-only: nothing here writes. The one write the PQ API makes to the asset tables (a PENDING
`og.calibration_attempt`) goes through `opengrid.assets.service.AssetHealthService.request_calibration`.
Rows are returned as plain dicts (JSON-encoded by FastAPI) except where an existing typed reader is reused.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from opengrid.assets.repo import PgWorkOrderRepo
from opengrid.core.models.pq import PqWaveformSummaryRow
from opengrid.core.pq import ComplianceState, PqEnvelopeLimits, PqMeasurement, worst_verdict
from opengrid.pq_ingest.aggregation import bank_measurement
from opengrid.pq_ingest.pg_backend import PgPqIngestBackend

#: Overall/per-bank compliance verdict when no fresh measurement exists (K1: missing is never compliant).
NO_DATA = "NO_DATA"


@dataclass(frozen=True, slots=True)
class HubLocation:
    """Where a hub sits in the topic tree (`<root>/scada/wave/<zone>/<bank_id>/<hub_id>/...`, S6.4)."""

    hub_id: str
    zone: str
    bank_id: str


@dataclass(frozen=True, slots=True)
class ObligationEnvelope:
    """An obligation, the PQ envelope its contract's latest service profile binds (S1/S2; `None` when
    the contract has no service profile yet), and the banks it currently holds reservations on."""

    obligation_id: UUID
    contract_id: UUID
    service_type: str
    state: str
    limits: PqEnvelopeLimits | None
    bank_ids: list[str]


class PqStore(Protocol):
    async def hub_location(self, hub_id: str) -> HubLocation | None: ...

    async def summaries(self, hub_ids: Sequence[str], *, since: datetime) -> list[PqWaveformSummaryRow]:
        """Waveform summaries for `hub_ids` with `ts >= since`, newest first per hub."""
        ...

    async def raw_captures(self, hub_id: str, *, until: datetime, limit: int) -> list[dict[str, Any]]:
        """`og.pq_waveform_raw_index` rows for `hub_id` with `ts <= until`, newest first."""
        ...

    async def hub_ids_for_bank(self, bank_id: str) -> list[str]: ...

    async def obligation_envelope(self, obligation_id: UUID) -> ObligationEnvelope | None: ...

    async def hub_inverter_pq(self, hub_id: str) -> dict[str, Any] | None: ...

    async def asset_events(self, hub_id: str, *, limit: int) -> list[dict[str, Any]]: ...

    async def open_work_order(self, hub_id: str) -> dict[str, Any] | None: ...

    async def calibration_history(self, hub_id: str, *, limit: int) -> list[dict[str, Any]]:
        """`og.calibration_attempt` rows, newest first, each with the guardian's decision on it
        (`og.calibration_command.status`: RESERVED/SIGNED/REFUSED, or `None` while undecided)."""
        ...

    async def work_orders(self, *, status: str | None, limit: int) -> list[dict[str, Any]]: ...


_HUB_LOCATION_SQL = "SELECT hub_id, zone, bank_id FROM og.hub WHERE hub_id = %(hub_id)s"

_RAW_CAPTURES_SQL = """
SELECT capture_id, hub_id, ts, trigger_reason, blob_ref, channels, sample_rate_hz, cycles, retain_until,
       created_at
FROM og.pq_waveform_raw_index
WHERE hub_id = %(hub_id)s AND ts <= %(until)s
ORDER BY ts DESC
LIMIT %(limit)s
"""

_HUB_IDS_FOR_BANK_SQL = "SELECT hub_id FROM og.hub WHERE bank_id = %(bank_id)s ORDER BY hub_id"

_OBLIGATION_ENVELOPE_SQL = """
SELECT ob.obligation_id, ob.contract_id, ob.service_type, ob.state, pe.max_phase_imbalance_pct,
       pe.voltage_band_pct, pe.freq_tolerance_hz, pe.pf_min, pe.thd_voltage_limit_pct,
       pe.thd_current_limit_pct, pe.current_limit_a, pe.pq_envelope_id
FROM og.obligation ob
LEFT JOIN LATERAL (
    SELECT sp.pq_envelope_id FROM og.service_profile sp
    WHERE sp.contract_id = ob.contract_id
    ORDER BY sp.version DESC
    LIMIT 1
) sp ON true
LEFT JOIN og.pq_envelope pe ON pe.pq_envelope_id = sp.pq_envelope_id
WHERE ob.obligation_id = %(obligation_id)s
"""

_OBLIGATION_BANKS_SQL = """
SELECT DISTINCT bank_id FROM og.reservation
WHERE obligation_id = %(obligation_id)s AND released_at IS NULL
ORDER BY bank_id
"""

_HUB_INVERTER_PQ_SQL = "SELECT * FROM og.hub_inverter_pq WHERE hub_id = %(hub_id)s"

_ASSET_EVENTS_SQL = """
SELECT asset_event_id, hub_id, work_order_id, event_type, from_state, to_state, old_inverter_serial,
       new_inverter_serial, old_firmware, new_firmware, reason_code, occurred_at
FROM og.asset_event WHERE hub_id = %(hub_id)s
ORDER BY occurred_at DESC
LIMIT %(limit)s
"""

_CALIBRATION_HISTORY_SQL = """
SELECT ca.calibration_id, ca.hub_id, ca.requested_at, ca.reference_phase_deg, ca.reference_freq_hz,
       ca.reference_amplitude_v, ca.measured_offset_freq_hz, ca.measured_offset_voltage_pct,
       ca.measured_offset_phase_deg, ca.correction_freq_hz, ca.correction_voltage_pct,
       ca.correction_phase_deg, ca.outcome, ca.verified_at,
       cc.status AS command_status, cc.reason AS command_reason, cc.signed_at, cc.ack_consumed_at
FROM og.calibration_attempt ca
LEFT JOIN og.calibration_command cc ON cc.calibration_id = ca.calibration_id
WHERE ca.hub_id = %(hub_id)s
ORDER BY ca.requested_at DESC
LIMIT %(limit)s
"""

_WORK_ORDERS_SQL = """
SELECT work_order_id, hub_id, severity, evidence, status, opened_at, closed_at, technician_notes
FROM og.maintenance_work_order
WHERE (%(status)s::text IS NULL OR status = %(status)s)
ORDER BY opened_at DESC
LIMIT %(limit)s
"""


class PgPqStore:
    """`PqStore` over the api process's own `AsyncConnectionPool` (`opengrid.api.deps.get_pool`)."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool
        self._summaries = PgPqIngestBackend(pool)
        self._work_orders = PgWorkOrderRepo(pool)

    async def _fetch_all(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, params)
            return list(await cur.fetchall())

    async def _fetch_one(self, query: str, params: dict[str, Any]) -> dict[str, Any] | None:
        rows = await self._fetch_all(query, params)
        return rows[0] if rows else None

    async def hub_location(self, hub_id: str) -> HubLocation | None:
        row = await self._fetch_one(_HUB_LOCATION_SQL, {"hub_id": hub_id})
        if row is None:
            return None
        return HubLocation(hub_id=row["hub_id"], zone=row["zone"], bank_id=row["bank_id"])

    async def summaries(self, hub_ids: Sequence[str], *, since: datetime) -> list[PqWaveformSummaryRow]:
        return await self._summaries.latest_summaries(hub_ids, since=since)

    async def raw_captures(self, hub_id: str, *, until: datetime, limit: int) -> list[dict[str, Any]]:
        return await self._fetch_all(_RAW_CAPTURES_SQL, {"hub_id": hub_id, "until": until, "limit": limit})

    async def hub_ids_for_bank(self, bank_id: str) -> list[str]:
        rows = await self._fetch_all(_HUB_IDS_FOR_BANK_SQL, {"bank_id": bank_id})
        return [row["hub_id"] for row in rows]

    async def obligation_envelope(self, obligation_id: UUID) -> ObligationEnvelope | None:
        params = {"obligation_id": obligation_id}
        row = await self._fetch_one(_OBLIGATION_ENVELOPE_SQL, params)
        if row is None:
            return None
        banks = await self._fetch_all(_OBLIGATION_BANKS_SQL, params)
        return ObligationEnvelope(
            obligation_id=row["obligation_id"],
            contract_id=row["contract_id"],
            service_type=row["service_type"],
            state=row["state"],
            limits=_limits_from_row(row),
            bank_ids=[str(b["bank_id"]) for b in banks],
        )

    async def hub_inverter_pq(self, hub_id: str) -> dict[str, Any] | None:
        return await self._fetch_one(_HUB_INVERTER_PQ_SQL, {"hub_id": hub_id})

    async def asset_events(self, hub_id: str, *, limit: int) -> list[dict[str, Any]]:
        return await self._fetch_all(_ASSET_EVENTS_SQL, {"hub_id": hub_id, "limit": limit})

    async def open_work_order(self, hub_id: str) -> dict[str, Any] | None:
        order = await self._work_orders.open_for_hub(hub_id)
        return order.model_dump() if order is not None else None

    async def calibration_history(self, hub_id: str, *, limit: int) -> list[dict[str, Any]]:
        return await self._fetch_all(_CALIBRATION_HISTORY_SQL, {"hub_id": hub_id, "limit": limit})

    async def work_orders(self, *, status: str | None, limit: int) -> list[dict[str, Any]]:
        return await self._fetch_all(_WORK_ORDERS_SQL, {"status": status, "limit": limit})


def _limits_from_row(row: dict[str, Any]) -> PqEnvelopeLimits | None:
    """`og.pq_envelope` columns -> `opengrid.core.pq.PqEnvelopeLimits`; `None` if no envelope is bound."""
    if row["pq_envelope_id"] is None:
        return None
    current_limit = row["current_limit_a"]
    return PqEnvelopeLimits(
        max_phase_imbalance_pct=float(row["max_phase_imbalance_pct"]),
        voltage_band_pct=float(row["voltage_band_pct"]),
        freq_tolerance_hz=float(row["freq_tolerance_hz"]),
        pf_min=float(row["pf_min"]),
        thd_voltage_limit_pct=float(row["thd_voltage_limit_pct"]),
        thd_current_limit_pct=float(row["thd_current_limit_pct"]),
        current_limit_a=float(current_limit) if current_limit is not None else None,
    )


async def measure_bank(
    store: PqStore, bank_id: str, *, freshness_s: float, require_hubs: bool
) -> tuple[dict[str, Any], PqMeasurement | None]:
    """`bank_measurement` over the bank's hubs' summaries in the last `freshness_s`: the response body
    and the raw `PqMeasurement` (for the compliance evaluation), `None` when nothing fresh reported."""
    hub_ids = await store.hub_ids_for_bank(bank_id)
    if require_hubs and not hub_ids:
        raise LookupError(f"unknown bank {bank_id} (no hubs)")
    now = datetime.now(UTC)
    summaries = await store.summaries(hub_ids, since=now - timedelta(seconds=freshness_s)) if hub_ids else []
    measurement = bank_measurement(summaries, now, freshness_s)
    body = {
        "bank_id": bank_id,
        "evaluated_at": now,
        "freshness_s": freshness_s,
        "hub_count": len(hub_ids),
        "reporting_hub_count": len({s.hub_id for s in summaries}),
        "measurement": asdict(measurement) if measurement is not None else None,
    }
    return body, measurement


def overall_verdict(verdicts: dict[str, ComplianceState], *, bank_count: int) -> str | None:
    """BREACH anywhere wins; otherwise a bank with no fresh data makes the whole answer NO_DATA (K1:
    missing is never compliant); otherwise the worst measured verdict. `None` with no reserved banks."""
    if bank_count == 0:
        return None
    worst = worst_verdict(verdicts)
    if worst is ComplianceState.BREACH or len(verdicts) == bank_count:
        return worst.value
    return NO_DATA
