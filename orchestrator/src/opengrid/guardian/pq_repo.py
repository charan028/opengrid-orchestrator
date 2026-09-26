"""Postgres implementations of `opengrid.guardian.pq_ports` (07-delivery/06-service-profiles-and-
power-quality.md S5.3/S5.4/S6.7, G-21..G-25) -- new file, additive, following `guardian/repo.py`'s own
style (kept psycopg-free logic split, BUILD.md S5a). `guardian/repo.py`/`service.py`/`ports.py`/`main.py`
are not edited here; the live-path agent wires these adapters into `build_pg_ports`/`main.py` (see the
wiring notes in the wave-2 build report).

Every port here is guardian's OWN, independent read -- it never trusts the allocator's/asset-health
ladder's claim that an envelope holds, that a hub has been substituted off a sensitive delivery, or what
asset state a hub is in (`pq_ports.py`'s own module docstring). Several queries below intentionally mirror
a query `opengrid.assets.repo` already runs for the SAME underlying fact (e.g. `PgSensitiveGrantPort`) --
per `opengrid.assets.ports.SensitiveGrantPort`'s own docstring, this is the K2/K14 "two separate reads of
the same ledger fact, primary + independent" pattern, not a duplicated implementation (each side owns its
own adapter; `orchestrator/tools/dupcheck.py` checks Python logic, not two independent SQL statements
expressing the same read for two different owners).
"""

from __future__ import annotations

import cmath
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from opengrid.assets.repo import PQ_SENSITIVE_SERVICE_TYPES
from opengrid.core.models.pq import PqWaveformSummaryRow
from opengrid.core.pq import (
    DEFAULT_FIRMWARE_CALIBRATION_BOUNDS,
    CalibrationBounds,
    PqEnvelopeLimits,
    PqMeasurement,
    bank_thd_current_pct,
)
from opengrid.guardian.pq_ports import CalibrationFleetUsage, HubAssetSnapshot
from opengrid.pq_ingest.aggregation import bank_measurement

logger = logging.getLogger(__name__)

# S4.a/S5.4's freshness gate: a summary older than 2x the profile's telemetry cadence is treated as
# missing, never as compliant (K1's stale-data pattern). `wave.WaveConfig.summary_interval_s`'s new
# MVP-scale default is 10 s (S9 wave-2 build report), so this defaults to 20 s -- callers on a tighter
# per-contract cadence should pass their own value rather than relying on this default.
# R3 (owner, 2026-09-26): summaries every 30 s to cut disk load, so the default is 2 x 30 s.
DEFAULT_FRESHNESS_S = 60.0

# S5.2 step 1 / S3.2(a): the tightest active envelope's own field is a hard UPPER bound (imbalance,
# voltage band, freq tolerance, THD) except pf_min, which is a hard LOWER bound (a smaller pf_min is
# LESS strict) -- "tightest" therefore means MIN for every field except pf_min, where it means MAX.

_TIGHTEST_ENVELOPE_SQL = """
SELECT pe.max_phase_imbalance_pct, pe.voltage_band_pct, pe.freq_tolerance_hz, pe.pf_min,
       pe.thd_voltage_limit_pct, pe.thd_current_limit_pct, pe.current_limit_a
FROM og.reservation r
JOIN og.obligation ob ON ob.obligation_id = r.obligation_id
JOIN LATERAL (
    SELECT sp.pq_envelope_id
    FROM og.service_profile sp
    WHERE sp.contract_id = ob.contract_id
    ORDER BY sp.version DESC
    LIMIT 1
) sp ON true
JOIN og.pq_envelope pe ON pe.pq_envelope_id = sp.pq_envelope_id
WHERE r.bank_id = %(bank_id)s AND r.released_at IS NULL
  AND r.interval_start <= now() AND r.interval_end > now()
  AND ob.service_type = ANY(%(service_types)s)
"""


class PgPqEnvelopeStatePort:
    """G-21..G-24's "tightest active limit among obligations served behind this bank" (S5.3). Only
    obligations on a PQ-sensitive service type (`opengrid.assets.repo.PQ_SENSITIVE_SERVICE_TYPES`, S4.b's
    `DATA_CENTER`/`PIPELINE_AC`) carry a non-default envelope in this MVP-S+ increment (S4.c: `HOME`/
    `ERCOT_ENERGY` are the fleet-default envelope and are unconstrained by K14) -- mirrors the same
    service-type convention `opengrid.assets.repo.PgSensitiveGrantPort` already uses, rather than
    introducing a second "is this the fleet default" marker on `og.pq_envelope` itself."""

    def __init__(
        self, pool: AsyncConnectionPool, *, service_types: frozenset[str] = PQ_SENSITIVE_SERVICE_TYPES
    ) -> None:
        self._pool = pool
        self._service_types = list(service_types)

    async def tightest_active_limits(self, bank_id: str) -> PqEnvelopeLimits | None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _TIGHTEST_ENVELOPE_SQL, {"bank_id": bank_id, "service_types": self._service_types}
            )
            rows = await cur.fetchall()
        if not rows:
            return None

        imbalance = min(float(r[0]) for r in rows)
        voltage_band = min(float(r[1]) for r in rows)
        freq_tolerance = min(float(r[2]) for r in rows)
        pf_min = max(float(r[3]) for r in rows)
        thd_voltage = min(float(r[4]) for r in rows)
        thd_current = min(float(r[5]) for r in rows)
        current_limits = [float(r[6]) for r in rows if r[6] is not None]
        return PqEnvelopeLimits(
            max_phase_imbalance_pct=imbalance,
            voltage_band_pct=voltage_band,
            freq_tolerance_hz=freq_tolerance,
            pf_min=pf_min,
            thd_voltage_limit_pct=thd_voltage,
            thd_current_limit_pct=thd_current,
            current_limit_a=min(current_limits) if current_limits else None,
        )


_HUB_IDS_FOR_BANK_SQL = "SELECT hub_id FROM og.hub WHERE bank_id = %(bank_id)s"

_HUB_INVERTER_PQ_FOR_BANK_SQL = """
SELECT h.hub_id, hip.freq_offset_hz, hip.voltage_offset_pct, hip.thd_current_pct, hip.pf_min_leading,
       hip.pf_min_lagging, hip.dominant_harmonics
FROM og.hub h
JOIN og.hub_inverter_pq hip ON hip.hub_id = h.hub_id
WHERE h.bank_id = %(bank_id)s
"""


def _modelled_fallback_measurement(rows: Sequence[tuple[Any, ...]]) -> PqMeasurement | None:
    """S3's pre-delivery/forecast-cross-check aggregation (S3.2), used here ONLY as G-21..G-23's
    conservative fallback when no measured waveform summary is fresh enough (S4.a: "the modelled S3.2
    estimate ... is used conservatively as a fallback"). `og.hub_inverter_pq` characterizes nameplate
    imperfection, not live per-cycle dispatch current, so this is necessarily a coarser, more
    conservative cross-check than the measured pipeline -- exactly the role S3's own front matter
    assigns it, never the delivery-time source of truth. Per S4.a's "assumes worst-case phase alignment
    when harmonics phase data is stale or missing", a hub with no `dominant_harmonics` contributes its
    THD via scalar (stacking-worst-case) addition rather than being dropped."""
    if not rows:
        return None

    freq_offsets = [float(r[1]) for r in rows]
    voltage_offsets = [float(r[2]) for r in rows]
    thd_values = [float(r[3]) for r in rows]
    pf_leading = [float(r[4]) for r in rows]
    pf_lagging = [float(r[5]) for r in rows]

    vector_hubs = [r for r in rows if r[6]]
    if len(vector_hubs) >= 2:
        per_hub_harmonics = [
            {int(order): _harmonic_phasor(component) for order, component in r[6].items()}
            for r in vector_hubs
        ]
        # Nameplate current is not modelled per-hub here (S3.2(b) needs a fundamental-current base);
        # weight every hub equally (1.0 A) so the result is a THD RATIO, not an absolute current.
        thd_current_pct = bank_thd_current_pct(per_hub_harmonics, [1.0] * len(vector_hubs))
    else:
        thd_current_pct = float(sum(thd_values))  # conservative worst-case scalar stacking (S4.a)

    return PqMeasurement(
        imbalance_pct=0.0,  # S3.2(c) needs live per-phase dispatch current, unavailable to this table
        voltage_deviation_pct=abs(sum(voltage_offsets) / len(voltage_offsets)),
        freq_deviation_hz=abs(sum(freq_offsets) / len(freq_offsets)),
        pf=min(min(pf_leading), min(pf_lagging)),
        thd_voltage_pct=0.0,  # S3.1 characterizes current THD only; voltage THD needs measured data
        thd_current_pct=thd_current_pct,
        current_a=None,
    )


def _harmonic_phasor(component: dict[str, float]) -> complex:
    mag = float(component.get("mag_pct", 0.0))
    angle = float(component.get("angle_deg", 0.0))
    return cmath.rect(mag, cmath.pi * angle / 180.0)


class PgPqMeasurementPort:
    """S6.5 step 3/4: guardian's own independent read of the measured per-bank aggregate (never the
    allocator's self-check value, S5.3's K2-pattern "primary at the allocator, independent at the
    guardian"), falling back to the S3.2 modelled estimate from `og.hub_inverter_pq` when no measured
    summary is fresh enough (S4.a)."""

    def __init__(self, pool: AsyncConnectionPool, *, freshness_s: float = DEFAULT_FRESHNESS_S) -> None:
        self._pool = pool
        self._freshness_s = freshness_s

    async def aggregate_measurement(self, bank_id: str) -> tuple[PqMeasurement, bool]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_HUB_IDS_FOR_BANK_SQL, {"bank_id": bank_id})
            hub_ids = [row[0] for row in await cur.fetchall()]

        summaries = await self._latest_summaries(hub_ids)
        now = datetime.now(UTC)
        measured = bank_measurement(summaries, now, self._freshness_s)
        if measured is not None:
            return measured, False

        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_HUB_INVERTER_PQ_FOR_BANK_SQL, {"bank_id": bank_id})
            rows = await cur.fetchall()
        fallback = _modelled_fallback_measurement(rows)
        if fallback is None:
            # Nothing measured AND nothing characterized: report the most conservative reading (every
            # dimension at its worst, K1's "missing -> never compliant") rather than raising, so G-21..
            # G-23 still runs and VETOes on the total absence of data.
            fallback = PqMeasurement(
                imbalance_pct=100.0,
                voltage_deviation_pct=100.0,
                freq_deviation_hz=100.0,
                pf=0.0,
                thd_voltage_pct=100.0,
                thd_current_pct=100.0,
            )
        return fallback, True

    async def _latest_summaries(self, hub_ids: Sequence[str]) -> list[PqWaveformSummaryRow]:
        if not hub_ids:
            return []
        since = datetime.now(UTC).timestamp() - self._freshness_s
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT DISTINCT ON (hub_id) hub_id, ts, v_rms_a, v_rms_b, v_rms_c, i_rms_a, i_rms_b,
                       i_rms_c, freq_hz, pf_a, pf_b, pf_c, thd_v_pct_a, thd_v_pct_b, thd_v_pct_c,
                       thd_i_pct_a, thd_i_pct_b, thd_i_pct_c, phase_angle_deg_a, phase_angle_deg_b,
                       phase_angle_deg_c, harmonics_v, harmonics_i, sync_source, sync_quality_ns
                FROM og.pq_waveform_summary
                WHERE hub_id = ANY(%(hub_ids)s) AND ts >= to_timestamp(%(since)s)
                ORDER BY hub_id, ts DESC
                """,
                {"hub_ids": list(hub_ids), "since": since},
            )
            rows = await cur.fetchall()
        columns = (
            "hub_id",
            "ts",
            "v_rms_a",
            "v_rms_b",
            "v_rms_c",
            "i_rms_a",
            "i_rms_b",
            "i_rms_c",
            "freq_hz",
            "pf_a",
            "pf_b",
            "pf_c",
            "thd_v_pct_a",
            "thd_v_pct_b",
            "thd_v_pct_c",
            "thd_i_pct_a",
            "thd_i_pct_b",
            "thd_i_pct_c",
            "phase_angle_deg_a",
            "phase_angle_deg_b",
            "phase_angle_deg_c",
            "harmonics_v",
            "harmonics_i",
            "sync_source",
            "sync_quality_ns",
        )
        return [PqWaveformSummaryRow(**dict(zip(columns, row, strict=True))) for row in rows]


_HUB_ASSET_STATE_SQL = (
    "SELECT asset_state, ride_through_class FROM og.hub_inverter_pq WHERE hub_id = %(hub_id)s"
)


class PgHubAssetStatePort:
    """G-24/asset-eligibility (S5.3, S5.5.3): guardian's own read of `og.hub_inverter_pq`'s asset-health
    columns, independent of `opengrid.assets.service.AssetHealthService`'s in-process view."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def snapshot(self, hub_id: str) -> HubAssetSnapshot | None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_HUB_ASSET_STATE_SQL, {"hub_id": hub_id})
            row = await cur.fetchone()
        if row is None:
            return None
        asset_state, ride_through_class = row
        return HubAssetSnapshot(asset_state=asset_state, ride_through_class=ride_through_class)


# The guardian's OWN record of calibration commands it signed: `og.calibration_command` (migration 0016),
# written only by og-guardian (`PgCalibrationLedgerPort`).
_LAST_SIGNED_CALIBRATION_SQL = """
SELECT extract(epoch FROM max(signed_at)) FROM og.calibration_command
WHERE hub_id = %(hub_id)s AND status = 'SIGNED'
"""


class PgCalibrationHistoryPort:
    """G-25's rate-limit re-check (S5.5.4, default 1/24h) on the guardian's own signed-command record,
    independent of the ladder's `og.calibration_attempt` bookkeeping (see
    `pq_ports.CalibrationHistoryPort`): what the rate limit protects is the inverter, and only a
    guardian-signed command ever reaches it."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def last_attempt_epoch_s(self, hub_id: str) -> float | None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_LAST_SIGNED_CALIBRATION_SQL, {"hub_id": hub_id})
            row = await cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None


#: Calibration commands use one sequence generation per hub; `seq` is the durable per-hub counter.
CALIBRATION_EPOCH = 1

_HUB_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('og.calibration_command:' || %(hub_id)s))"

# The INSERT is the atomic claim of the attempt (primary key): ON CONFLICT DO NOTHING returns no row when
# it was already claimed. The per-hub advisory lock serialises seq assignment; UNIQUE(hub_id, epoch, seq)
# is the backstop.
_RESERVE_SQL = """
INSERT INTO og.calibration_command (calibration_id, hub_id, status, epoch, seq)
SELECT %(calibration_id)s, %(hub_id)s, 'RESERVED', %(epoch)s, COALESCE(MAX(seq), 0) + 1
FROM og.calibration_command WHERE hub_id = %(hub_id)s AND epoch = %(epoch)s
ON CONFLICT (calibration_id) DO NOTHING
RETURNING epoch, seq
"""

_MARK_SIGNED_SQL = """
UPDATE og.calibration_command SET status = 'SIGNED', signed_at = now()
WHERE calibration_id = %(calibration_id)s AND status = 'RESERVED'
"""

_REFUSE_SQL = """
INSERT INTO og.calibration_command (calibration_id, hub_id, status, reason)
VALUES (%(calibration_id)s, %(hub_id)s, 'REFUSED', %(reason)s)
ON CONFLICT (calibration_id) DO NOTHING
"""

_FLEET_USAGE_SQL = """
SELECT
  (SELECT count(*) FROM og.hub),
  (SELECT count(*) FROM og.calibration_command
    WHERE status = 'SIGNED' AND signed_at > now() - make_interval(secs => %(window_s)s)),
  (SELECT count(*) FROM og.calibration_command
    WHERE status = 'SIGNED' AND ack_consumed_at IS NULL AND signed_at > now() - make_interval(secs => %(window_s)s)),
  (SELECT count(DISTINCT hub_id) FROM og.calibration_attempt
    WHERE requested_at > now() - make_interval(secs => %(window_s)s))
"""

_OPEN_ALERT_SQL = "SELECT 1 FROM og.alert WHERE rule = %(rule)s AND cleared_at IS NULL LIMIT 1"


class PgCalibrationLedgerPort:
    """`og.calibration_command`: the atomic claim of an attempt, the durable per-hub (epoch, seq), and the
    G-25 fleet usage. Alerts go through `opengrid.health.queries.raise_alert` (the single `og.alert`
    writer), at most once while an alert with the same rule is open."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def reserve(self, calibration_id: UUID, hub_id: str) -> tuple[int, int] | None:
        params = {"calibration_id": calibration_id, "hub_id": hub_id, "epoch": CALIBRATION_EPOCH}
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_HUB_LOCK_SQL, {"hub_id": hub_id})
            await cur.execute(_RESERVE_SQL, params)
            row = await cur.fetchone()
            await conn.commit()
        return (int(row[0]), int(row[1])) if row else None

    async def mark_signed(self, calibration_id: UUID) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_MARK_SIGNED_SQL, {"calibration_id": calibration_id})
            await conn.commit()

    async def refuse(self, calibration_id: UUID, hub_id: str, reason: str) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _REFUSE_SQL, {"calibration_id": calibration_id, "hub_id": hub_id, "reason": reason}
            )
            await conn.commit()

    async def fleet_usage(self, *, window_s: float) -> CalibrationFleetUsage:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_FLEET_USAGE_SQL, {"window_s": window_s})
            row = await cur.fetchone()
        if row is None:
            return CalibrationFleetUsage(0, 0, 0, 0)
        return CalibrationFleetUsage(int(row[0]), int(row[1]), int(row[2]), int(row[3]))

    async def raise_alert(self, rule: str, summary: str, detail: dict[str, object]) -> None:
        from opengrid.health.model import AlertFinding
        from opengrid.health.queries import raise_alert

        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_OPEN_ALERT_SQL, {"rule": rule})
            if await cur.fetchone() is not None:
                return
        finding = AlertFinding(
            rule=rule, severity="warning", summary=summary, condition_key=rule, detail=dict(detail)
        )
        await raise_alert(self._pool, finding, opened_at=datetime.now(UTC))


class StaticFirmwareCalibrationBoundsPort:
    """G-25's defense-in-depth bound (S6.7): the maximum correction magnitude a `CalibrationCommand` may
    carry, independent of whatever bound the candidate command itself claims. MVP-S+'s schema
    (`0011_asset_health.sql`) has no per-firmware-family bounds table yet -- this adapter returns one
    fixed, conservative bound for every hub, which is REPORTED as a known gap (wave-2 build report: a
    dedicated `og.inverter_firmware_bounds` table, keyed by firmware family, is the natural next
    migration once real firmware-family data exists) rather than guessed at silently (BUILD.md S5a "no
    silent fallbacks"). The defaults mirror `calibration_command.schema.json`'s own `bounds` fields'
    units and are deliberately tight (a remote correction is a small trim, never a large swing). The
    default is the single canonical `opengrid.core.pq.DEFAULT_FIRMWARE_CALIBRATION_BOUNDS`."""

    def __init__(self, bounds: CalibrationBounds = DEFAULT_FIRMWARE_CALIBRATION_BOUNDS) -> None:
        self._bounds = bounds

    async def max_bounds_for_hub(self, hub_id: str) -> CalibrationBounds:
        _ = hub_id  # same fixed bound for every hub until a firmware-family table exists (see docstring)
        return self._bounds


_ACTIVE_SENSITIVE_GRANT_SQL = """
SELECT 1
FROM og.trace t
JOIN LATERAL jsonb_array_elements(t.payload -> 'items') AS item ON true
JOIN og.obligation ob ON ob.obligation_id = (item ->> 'obligation_id')::uuid
WHERE t.decision_type = 'RT_ALLOCATION'
  AND item ->> 'hub_id' = %(hub_id)s
  AND ob.service_type = ANY(%(service_types)s)
  AND ob.state IN ('COMMITTED', 'DELIVERING')
  AND t.created_at >= now() - interval '1 hour'
LIMIT 1
"""


class PgSensitiveGrantPort:
    """G-25(iii)/S5.5.4's independent re-check ("never assumes the ladder's step-2 substitution already
    ran"). Deliberately the SAME query shape as `opengrid.assets.repo.PgSensitiveGrantPort` -- see this
    module's own docstring and `opengrid.assets.ports.SensitiveGrantPort`'s docstring for why that
    mirroring is the intended K2/K14 primary+independent pattern, not duplication to fix."""

    def __init__(
        self, pool: AsyncConnectionPool, *, service_types: frozenset[str] = PQ_SENSITIVE_SERVICE_TYPES
    ) -> None:
        self._pool = pool
        self._service_types = list(service_types)

    async def has_active_non_default_envelope_grant(self, hub_id: str) -> bool:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _ACTIVE_SENSITIVE_GRANT_SQL, {"hub_id": hub_id, "service_types": self._service_types}
            )
            row = await cur.fetchone()
        return row is not None
