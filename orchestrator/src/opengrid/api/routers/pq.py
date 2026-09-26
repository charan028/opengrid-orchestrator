"""Power-quality and asset-health API (07-delivery/06 S6.6/S6.7, WP-J; traceability ES15/TS-15a).

Reads (viewer): the latest waveform summary plus raw-capture metadata, the harmonic spectrum, a bank's
measured PQ aggregate, an obligation's PQ compliance, a hub's asset health, its calibration history,
and maintenance work orders. Writes (operator, two-step propose/confirm like safe stop, S6.7 "never a
single accidental click"): an on-demand waveform capture, and an operator-initiated calibration.

No PQ math lives here (BUILD.md S1): the bank aggregate is `opengrid.pq_ingest.aggregation.
bank_measurement`, compliance is `opengrid.core.pq.evaluate_envelope`/`compliance_ratios`/
`worst_verdict`, the capture request is `opengrid.pq_ingest.capture.build_capture_request`, and the
calibration candidate (plus its ladder-side primary checks: sensitive grant, 24h rate limit, fresh drift
measurement) is `opengrid.assets.service.AssetHealthService.request_calibration`.

K3: the API never signs. A confirmed calibration only writes the PENDING `og.calibration_attempt` row
(through `AssetHealthService`); `og-guardian` evaluates G-25 on it and signs or refuses, and the outcome
shows up in `GET .../calibration-history`.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

import aiomqtt
from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field

from opengrid.api.auth import Identity, require_operator, require_viewer
from opengrid.api.deps import get_config, get_pool, get_proposals, get_store, get_trace_store
from opengrid.api.pq_capture_limits import CaptureRateLimitedError, CaptureRateLimiter, get_capture_limiter
from opengrid.api.pq_store import NO_DATA, HubLocation, PgPqStore, PqStore, measure_bank, overall_verdict
from opengrid.api.proposals import ProposalExpiredError, ProposalStore
from opengrid.api.schemas import ProposalAccepted
from opengrid.api.store import StoreProtocol
from opengrid.assets.calibration import CalibrationReference
from opengrid.assets.runner import DEFAULT_CALIBRATION_BOUNDS, DEFAULT_CALIBRATION_LEASE_TTL_S
from opengrid.assets.service import AssetHealthService
from opengrid.assets.wiring import build_asset_health_service
from opengrid.core.pq import ComplianceState, compliance_ratios, evaluate_envelope, worst_verdict
from opengrid.core.pq.constants import NOMINAL_FREQ_HZ
from opengrid.platform.config import Config
from opengrid.platform.mqtt import build_client, topic, validate_payload
from opengrid.pq_ingest.aggregation import NOMINAL_VOLTAGE_V
from opengrid.pq_ingest.capture import build_capture_request
from opengrid.trace.store import TraceStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/og/api", tags=["pq"])

#: S5.4's freshness gate: a summary older than 2x the telemetry cadence is missing, never compliant (K1).
#: 20 s = 2x the 10 s summary cadence; the same default `opengrid.guardian.pq_repo` uses for G-21..G-23.
DEFAULT_FRESHNESS_S = 20.0
#: How far back `GET .../waveform?at=` looks for the latest summary at or before `at`.
DEFAULT_WAVEFORM_LOOKBACK_S = 900.0
#: Default window for `GET .../spectrum` when `from` is not given (S5.5.1's 15-minute observation window).
DEFAULT_SPECTRUM_WINDOW_S = 900.0
MAX_LIST_LIMIT = 500
_PROPOSAL_TTL_S = 60.0
_CAPTURE_PROPOSAL_KIND = "pq-waveform-capture"
_CALIBRATE_PROPOSAL_KIND = "pq-calibrate"
_MQTT_API_PASSWORD_ENV = "OG_MQTT_API_PASSWORD"  # noqa: S105 -- an env-var *name*, never a secret value

#: Publishes one JSON payload to a topic suffix under `[mqtt].topic_root`.
CapturePublisher = Callable[[str, dict[str, Any]], Awaitable[None]]


class PqActionRequest(BaseModel):
    """Step-1 body for both operator writes: why the operator is asking (recorded in the audit trail)."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


# -- dependencies (overridable in tests via `app.dependency_overrides`) ---------------------------------


def get_pq_store(pool: Annotated[AsyncConnectionPool, Depends(get_pool)]) -> PqStore:
    return PgPqStore(pool)


def get_asset_health_service(
    pool: Annotated[AsyncConnectionPool, Depends(get_pool)],
    trace_store: Annotated[TraceStore, Depends(get_trace_store)],
) -> AssetHealthService:
    return build_asset_health_service(pool, trace_store)


def get_capture_publisher(cfg: Annotated[Config, Depends(get_config)]) -> CapturePublisher:
    """The api process's existing MQTT publish path (`og_api` credentials, as `routers.scenario`)."""

    async def publish(topic_suffix: str, payload: dict[str, Any]) -> None:
        password = os.environ.get(_MQTT_API_PASSWORD_ENV, "")
        try:
            async with build_client(cfg, username="og_api", password=password, process="api-pq") as client:
                await client.publish(topic(cfg, topic_suffix), payload=json.dumps(payload).encode(), qos=1)
        except aiomqtt.MqttError as exc:
            logger.warning("waveform capture request publish failed", extra={"error": str(exc)})
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail="MQTT broker unavailable; capture not requested"
            ) from exc

    return publish


PqStoreDep = Annotated[PqStore, Depends(get_pq_store)]
ViewerDep = Annotated[Identity, Depends(require_viewer)]
OperatorDep = Annotated[Identity, Depends(require_operator)]
LimitQuery = Annotated[int, Query(gt=0, le=MAX_LIST_LIMIT)]


# -- reads (viewer) --------------------------------------------------------------------------------------


@router.get("/hubs/{hub_id}/waveform")
async def hub_waveform(
    hub_id: str,
    store: PqStoreDep,
    _identity: ViewerDep,
    at: datetime | None = None,
    lookback_s: Annotated[float, Query(gt=0, le=86_400)] = DEFAULT_WAVEFORM_LOOKBACK_S,
    limit: LimitQuery = 10,
) -> dict[str, Any]:
    """S6.6: the latest `og.pq_waveform_summary` at or before `at` (default now, within `lookback_s`)
    plus the newest `og.pq_waveform_raw_index` capture metadata. `summary` is `null` when nothing was
    reported in the window (never a synthesized reading). Use `POST .../waveform-capture` for a fresh one."""
    await _require_hub(store, hub_id)
    until = _as_utc(at) if at is not None else datetime.now(UTC)
    rows = await store.summaries([hub_id], since=until - timedelta(seconds=lookback_s))
    in_window = [row for row in rows if row.hub_id == hub_id and row.ts <= until]
    latest = max(in_window, key=lambda row: row.ts, default=None)
    captures = await store.raw_captures(hub_id, until=until, limit=limit)
    return {
        "hub_id": hub_id,
        "at": until,
        "summary": latest.model_dump(mode="json") if latest is not None else None,
        "raw_captures": captures,
    }


@router.get("/hubs/{hub_id}/spectrum")
async def hub_spectrum(
    hub_id: str,
    store: PqStoreDep,
    _identity: ViewerDep,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
) -> dict[str, Any]:
    """S6.6: harmonic-order time series (order -> magnitude % of fundamental, angle) from summaries in
    [`from`, `to`] (default the last 15 minutes), oldest first."""
    await _require_hub(store, hub_id)
    until = _as_utc(to) if to is not None else datetime.now(UTC)
    since = _as_utc(from_) if from_ is not None else until - timedelta(seconds=DEFAULT_SPECTRUM_WINDOW_S)
    if since > until:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="'from' must not be after 'to'")
    rows = await store.summaries([hub_id], since=since)
    points = sorted((row for row in rows if row.hub_id == hub_id and row.ts <= until), key=lambda r: r.ts)
    return {
        "hub_id": hub_id,
        "from": since,
        "to": until,
        "points": [
            {
                "ts": row.ts,
                "harmonics_v": _dump_harmonics(row.harmonics_v),
                "harmonics_i": _dump_harmonics(row.harmonics_i),
            }
            for row in points
        ],
    }


@router.get("/banks/{bank_id}/pq")
async def bank_pq(
    bank_id: str,
    store: PqStoreDep,
    _identity: ViewerDep,
    freshness_s: Annotated[float, Query(gt=0, le=3_600)] = DEFAULT_FRESHNESS_S,
) -> dict[str, Any]:
    """S6.5 step 3: the measured per-bank aggregate (`bank_measurement`). `measurement` is `null` when no
    hub on the bank has a fresh summary with voltage and frequency (K1: missing is never compliant)."""
    body, _measurement = await measure_bank(store, bank_id, freshness_s=freshness_s, require_hubs=True)
    return body


@router.get("/obligations/{obligation_id}/pq-compliance")
async def obligation_pq_compliance(
    obligation_id: UUID,
    store: PqStoreDep,
    _identity: ViewerDep,
    freshness_s: Annotated[float, Query(gt=0, le=3_600)] = DEFAULT_FRESHNESS_S,
) -> dict[str, Any]:
    """S5.4/S6.6: the obligation's bound PQ envelope checked against the measured aggregate of every bank
    it currently holds reservations on. Per bank: per-dimension verdicts and limit ratios; overall: the
    worst verdict. A bank with no fresh measurement is reported `NO_DATA`, never `NOMINAL` (K1)."""
    envelope = await store.obligation_envelope(obligation_id)
    if envelope is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"unknown obligation {obligation_id}")
    result: dict[str, Any] = {
        "obligation_id": envelope.obligation_id,
        "contract_id": envelope.contract_id,
        "service_type": envelope.service_type,
        "state": envelope.state,
        "envelope": asdict(envelope.limits) if envelope.limits is not None else None,
        "evaluated_at": datetime.now(UTC),
        "banks": [],
        "overall": None,
    }
    if envelope.limits is None:
        return result | {"detail": "no service profile/PQ envelope bound to this obligation's contract"}

    verdicts: dict[str, ComplianceState] = {}
    for bank_id in envelope.bank_ids:
        bank, measurement = await measure_bank(store, bank_id, freshness_s=freshness_s, require_hubs=False)
        if measurement is None:
            result["banks"].append(bank | {"verdict": NO_DATA})
            continue
        per_dimension = evaluate_envelope(measurement, envelope.limits)
        verdicts[bank_id] = worst_verdict(per_dimension)
        result["banks"].append(
            bank
            | {
                "verdict": verdicts[bank_id].value,
                "dimensions": {k: v.value for k, v in per_dimension.items()},
                "ratios": compliance_ratios(measurement, envelope.limits),
            }
        )
    result["overall"] = overall_verdict(verdicts, bank_count=len(envelope.bank_ids))
    return result


@router.get("/hubs/{hub_id}/asset-health")
async def hub_asset_health(
    hub_id: str, store: PqStoreDep, _identity: ViewerDep, limit: LimitQuery = 50
) -> dict[str, Any]:
    """S6.7: the hub's `og.hub_inverter_pq` row (characterization plus asset state), its recent
    `og.asset_event` history, and its open maintenance work order, if any."""
    characterization = await store.hub_inverter_pq(hub_id)
    if characterization is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no inverter PQ record for hub {hub_id}")
    return {
        "hub_id": hub_id,
        "asset_state": characterization.get("asset_state"),
        "inverter_pq": characterization,
        "history": await store.asset_events(hub_id, limit=limit),
        "open_work_order": await store.open_work_order(hub_id),
    }


@router.get("/hubs/{hub_id}/calibration-history")
async def hub_calibration_history(
    hub_id: str, store: PqStoreDep, _identity: ViewerDep, limit: LimitQuery = 50
) -> dict[str, Any]:
    """S6.7: `og.calibration_attempt` rows for the hub, newest first, with the guardian's G-25 decision
    (`command_status`) joined from `og.calibration_command`."""
    await _require_hub(store, hub_id)
    return {"hub_id": hub_id, "attempts": await store.calibration_history(hub_id, limit=limit)}


@router.get("/work-orders")
async def list_work_orders(
    store: PqStoreDep,
    _identity: ViewerDep,
    status_: Annotated[
        str | None, Query(alias="status", pattern="^(OPEN|IN_PROGRESS|CLOSED|CANCELLED)$")
    ] = None,
    limit: LimitQuery = 100,
) -> list[dict[str, Any]]:
    """S6.7: `og.maintenance_work_order` rows, newest first, optionally filtered by `status`."""
    return await store.work_orders(status=status_, limit=limit)


# -- writes (operator, two-step) -------------------------------------------------------------------------


@router.post("/hubs/{hub_id}/waveform-capture", status_code=status.HTTP_202_ACCEPTED)
async def propose_waveform_capture(
    hub_id: str,
    body: PqActionRequest,
    store: PqStoreDep,
    proposals: Annotated[ProposalStore, Depends(get_proposals)],
    identity: OperatorDep,
) -> ProposalAccepted:
    """Step 1 of 2 (S6.6): validates the hub and stores a proposal; nothing is published yet."""
    location = await _require_hub(store, hub_id)
    summary = f"Request an on-demand raw waveform capture from {hub_id} (bank {location.bank_id})"
    proposal = proposals.create(_CAPTURE_PROPOSAL_KIND, (hub_id, body.reason), summary, identity.user)
    return ProposalAccepted(proposal_id=proposal.proposal_id, summary=summary, expires_in_s=_PROPOSAL_TTL_S)


@router.post("/hubs/{hub_id}/waveform-capture/{proposal_id}/confirm")
async def confirm_waveform_capture(
    hub_id: str,
    proposal_id: UUID,
    store: PqStoreDep,
    proposals: Annotated[ProposalStore, Depends(get_proposals)],
    publish: Annotated[CapturePublisher, Depends(get_capture_publisher)],
    api_store: Annotated[StoreProtocol, Depends(get_store)],
    trace_store: Annotated[TraceStore, Depends(get_trace_store)],
    limiter: Annotated[CaptureRateLimiter, Depends(get_capture_limiter)],
    identity: OperatorDep,
) -> dict[str, Any]:
    """Step 2 of 2 (S6.4b/S6.6, TS-15a): builds a `WaveformCaptureRequest` (`build_capture_request`,
    trigger `API_REQUEST`), validates it against `waveform_capture_request.schema.json`, and publishes it
    to `<root>/scada/wave/<zone>/<bank_id>/<hub_id>/request`. The capture arrives asynchronously through
    the engine's ingest path and appears in `GET .../waveform`'s `raw_captures`."""
    reason = _pop_proposal_for_hub(proposals, proposal_id, _CAPTURE_PROPOSAL_KIND, hub_id)
    location = await _require_hub(store, hub_id)
    try:  # 1 per hub per 60 s, 20 per minute fleet-wide (`api.pq_capture_limits`)
        limiter.acquire(hub_id)
    except CaptureRateLimitedError as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"waveform capture rate limit: {exc}",
            headers={"Retry-After": str(max(1, int(exc.retry_after_s) + 1))},
        ) from exc
    request = build_capture_request(hub_id, "API_REQUEST", now=datetime.now(UTC))
    payload = request.model_dump(mode="json")
    validate_payload("waveform_capture_request", payload)
    await publish(f"scada/wave/{location.zone}/{location.bank_id}/{hub_id}/request", payload)
    trace_id = await _record_operator_action(
        api_store,
        trace_store,
        identity,
        target_ref=f"hub:{hub_id}",
        reason=f"waveform capture requested ({reason})",
        payload={"request_id": str(request.request_id), "hub_id": hub_id},
    )
    return {
        "proposal_id": proposal_id,
        "request_id": request.request_id,
        "hub_id": hub_id,
        "expires_at": request.expires_at,
        "trace_id": trace_id,
    }


@router.post("/hubs/{hub_id}/calibrate", status_code=status.HTTP_202_ACCEPTED)
async def propose_calibration(
    hub_id: str,
    body: PqActionRequest,
    store: PqStoreDep,
    proposals: Annotated[ProposalStore, Depends(get_proposals)],
    identity: OperatorDep,
) -> ProposalAccepted:
    """Step 1 of 2 (S6.7): validates the hub and stores a proposal; nothing is recorded yet."""
    await _require_hub(store, hub_id)
    summary = (
        f"Request a remote calibration of {hub_id}: records a PENDING calibration attempt (correction = "
        "negative of the latest measured offset, clipped to firmware bounds) for the guardian to check "
        "(G-25) and sign or refuse"
    )
    proposal = proposals.create(_CALIBRATE_PROPOSAL_KIND, (hub_id, body.reason), summary, identity.user)
    return ProposalAccepted(proposal_id=proposal.proposal_id, summary=summary, expires_in_s=_PROPOSAL_TTL_S)


@router.post("/hubs/{hub_id}/calibrate/{proposal_id}/confirm", status_code=status.HTTP_202_ACCEPTED)
async def confirm_calibration(
    hub_id: str,
    proposal_id: UUID,
    store: PqStoreDep,
    proposals: Annotated[ProposalStore, Depends(get_proposals)],
    service: Annotated[AssetHealthService, Depends(get_asset_health_service)],
    api_store: Annotated[StoreProtocol, Depends(get_store)],
    trace_store: Annotated[TraceStore, Depends(get_trace_store)],
    identity: OperatorDep,
) -> dict[str, Any]:
    """Step 2 of 2 (S6.7): `AssetHealthService.request_calibration` builds the candidate and writes the
    PENDING `og.calibration_attempt` (traced `CALIBRATION_ATTEMPT`). `409` when the ladder's own primary
    checks refuse (a live PQ-sensitive grant on the hub, the 24h rate limit, or no fresh drift
    measurement). `202`: recorded, awaiting the guardian's G-25 decision; the API never signs (K3)."""
    reason = _pop_proposal_for_hub(proposals, proposal_id, _CALIBRATE_PROPOSAL_KIND, hub_id)
    await _require_hub(store, hub_id)
    now = datetime.now(UTC)
    candidate = await service.request_calibration(
        hub_id,
        reference=CalibrationReference(
            phase_deg=0.0,
            freq_hz=NOMINAL_FREQ_HZ,
            amplitude_v=NOMINAL_VOLTAGE_V,
            sync_source="ntp_disciplined",
        ),
        bounds=DEFAULT_CALIBRATION_BOUNDS,
        now=now,  # no epoch/seq: the guardian assigns them on og.calibration_command when it signs (#30)
        lease_ttl_s=DEFAULT_CALIBRATION_LEASE_TTL_S,
    )
    if candidate is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="calibration refused by the ladder's primary checks: a live PQ-sensitive delivery on "
            "the hub, the per-hub rate limit, or no fresh drift measurement to correct",
        )
    trace_id = await _record_operator_action(
        api_store,
        trace_store,
        identity,
        target_ref=f"hub:{hub_id}",
        reason=f"calibration requested ({reason})",
        payload={"calibration_id": str(candidate.calibration_id), "hub_id": hub_id},
    )
    return {
        "proposal_id": proposal_id,
        "calibration_id": candidate.calibration_id,
        "hub_id": hub_id,
        "outcome": "PENDING",
        "correction": asdict(candidate.correction),
        "detail": "recorded; awaiting the guardian's G-25 check and signature",
        "trace_id": trace_id,
    }


# -- helpers ---------------------------------------------------------------------------------------------


def _as_utc(value: datetime) -> datetime:
    """A query timestamp without an offset is read as UTC (every `og.*` timestamp is timestamptz)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _require_hub(store: PqStore, hub_id: str) -> HubLocation:
    location = await store.hub_location(hub_id)
    if location is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"unknown hub {hub_id}")
    return location


def _dump_harmonics(harmonics: Any) -> dict[str, Any] | None:
    if harmonics is None:
        return None
    return {order: component.model_dump(mode="json") for order, component in harmonics.items()}


def _pop_proposal_for_hub(proposals: ProposalStore, proposal_id: UUID, kind: str, hub_id: str) -> str:
    """Consumes the proposal (single use) and returns its reason; `404` if unknown or for another hub,
    `410` if its 60 s confirmation window has passed."""
    try:
        proposal = proposals.peek(proposal_id, kind=kind)
    except ProposalExpiredError as exc:
        raise HTTPException(status.HTTP_410_GONE, detail="proposal expired, propose again") from exc
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown proposal") from exc
    proposed_hub, reason = proposal.body
    if proposed_hub != hub_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown proposal for this hub")
    proposals.pop(proposal_id, kind=kind)
    return str(reason)


async def _record_operator_action(
    store: StoreProtocol,
    trace_store: TraceStore,
    identity: Identity,
    *,
    target_ref: str,
    reason: str,
    payload: dict[str, Any],
) -> UUID:
    """Every API write is recorded in `og.operator_action` + trace (BUILD.md api row)."""
    trace_ref = await trace_store.append(
        stream_id=f"operator_action:{identity.user}",
        decision_type="OPERATOR_ACTION",
        event_class="MANUAL_COMMAND",
        payload={"decision_ref": target_ref, "action": reason, **payload},
    )
    await store.insert_operator_action(
        operator_ref=identity.user,
        action_kind="MANUAL_COMMAND",
        target_ref=target_ref,
        tier=None,
        reason=reason,
        trace_id=trace_ref.trace_id,
        confirmed_at=datetime.now(UTC),
    )
    return trace_ref.trace_id
