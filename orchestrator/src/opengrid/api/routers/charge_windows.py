"""Owner grid-charging windows on the Fleet page (OWNER DECISION D-30), under `/og/api/fleet/charge-windows`.

- `GET    /og/api/fleet/charge-windows`                        (viewer)   every scope's windows
- `GET    /og/api/fleet/charge-windows/effective?hub_id=|bank_id=` (viewer) the windows that apply, and from
                                                                            which scope
- `PUT    /og/api/fleet/charge-windows/{scope_kind}/{scope_ref}` (operator) propose new windows
- `DELETE /og/api/fleet/charge-windows/{scope_kind}/{scope_ref}` (operator) propose removing an override
- `POST   /og/api/fleet/charge-windows/proposals/{proposal_id}/confirm` (operator) apply a proposal

Writes are two-step like every other operator write (`ProposalStore`); the confirm re-checks that the
scope still holds what the proposal showed as `old_windows` (409 otherwise), traces the change first
(K10, `OPERATOR_ACTION` / `CHARGE_WINDOW_CHANGE`, old -> new), then writes `og.owner_charge_window`
(migration 0038) and the `og.operator_action` row. Format and scope resolution are
`opengrid.core.charge_windows` (shared with the selector). Mounted on the fleet router
(`opengrid.api.routers.fleet`), so no separate app mount is needed.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from opengrid.api.auth import Identity, require_operator, require_viewer
from opengrid.api.deps import get_proposals, get_store, get_trace_store
from opengrid.api.proposals import PROPOSAL_TTL_S, ProposalExpiredError, ProposalStore
from opengrid.api.store import StoreProtocol
from opengrid.core.charge_windows import (
    CHARGE_WINDOW_TZ,
    MAX_WINDOWS,
    ChargeWindowError,
    Topology,
    provider_for_zone,
    resolve,
    validate_scope,
    validate_windows,
)
from opengrid.trace.store import TraceStore

router = APIRouter(prefix="/charge-windows", tags=["fleet"])

PROPOSAL_KIND = "charge_window"
EVENT_CLASS = "CHARGE_WINDOW_CHANGE"


class ChargeWindowPut(BaseModel):
    windows: list[str] = Field(max_length=MAX_WINDOWS)
    reason: str = Field(min_length=1, max_length=200)


class ChargeWindowDelete(BaseModel):
    reason: str = Field(min_length=1, max_length=200)


@dataclass(frozen=True, slots=True)
class _Change:
    scope_kind: str
    scope_ref: str
    old_windows: list[str] | None
    new_windows: list[str] | None  # None: remove the override
    reason: str


def _tdsp_tariffs_path() -> Path:
    """`OG_TDSP_TARIFFS_PATH`, else `tdsp_tariffs.toml` next to `OG_CONFIG` (settle's own resolution rule;
    not imported, because `opengrid.settle` is a heavy import for og-api)."""
    override = os.environ.get("OG_TDSP_TARIFFS_PATH")
    if override:
        return Path(override)
    og_config = os.environ.get("OG_CONFIG")
    return Path(og_config).parent / "tdsp_tariffs.toml" if og_config else Path("config/tdsp_tariffs.toml")


@cache
def _provider_tables() -> tuple[dict[str, str], dict[str, str]]:
    """(`{zone: utility}` from `[zone_territory]`, `{zone: tdsp}` from `[zone_default_tdsp]`); empty maps
    when the file is missing (the PROVIDER scope then simply never matches)."""
    try:
        with _tdsp_tariffs_path().open("rb") as fh:
            raw: dict[str, Any] = tomllib.load(fh)
    except OSError:
        return {}, {}
    utilities = {
        str(zone): str(entry["utility"])
        for zone, entry in raw.get("zone_territory", {}).items()
        if isinstance(entry, dict) and entry.get("utility")
    }
    tdsps = {str(zone): str(tdsp) for zone, tdsp in raw.get("zone_default_tdsp", {}).items()}
    return utilities, tdsps


def _row_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "scope_kind": row["scope_kind"],
        "scope_ref": row["scope_ref"],
        "windows": list(row["windows"]),
        "updated_by": row["updated_by"],
        "updated_at": row["updated_at"].isoformat(),
    }


async def _current(store: StoreProtocol, scope_kind: str, scope_ref: str) -> list[str] | None:
    for row in await store.list_charge_windows():
        if row["scope_kind"] == scope_kind and row["scope_ref"] == scope_ref:
            return list(row["windows"])
    return None


@router.get("")
async def list_charge_windows(
    store: Annotated[StoreProtocol, Depends(get_store)],
    _identity: Annotated[Identity, Depends(require_viewer)],
) -> dict[str, Any]:
    return {"tz": CHARGE_WINDOW_TZ, "items": [_row_payload(r) for r in await store.list_charge_windows()]}


@router.get("/effective")
async def effective_charge_windows(
    store: Annotated[StoreProtocol, Depends(get_store)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    hub_id: str | None = None,
    bank_id: str | None = None,
) -> dict[str, Any]:
    """The windows that apply to one hub (or bank): the most specific scope with a row wins."""
    if (hub_id is None) == (bank_id is None):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail="give exactly one of hub_id or bank_id"
        )
    topo_row = await store.charge_window_topology(hub_id=hub_id, bank_id=bank_id)
    if topo_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="hub or bank not found")
    utilities, tdsps = _provider_tables()
    topology = Topology(
        hub_id=topo_row.get("hub_id"),
        bank_id=topo_row.get("bank_id"),
        feeder_id=topo_row.get("feeder_id"),
        substation_id=topo_row.get("substation_id"),
        zone=topo_row.get("zone"),
        provider=provider_for_zone(topo_row.get("zone"), utilities, tdsps),
    )
    rows = {(r["scope_kind"], r["scope_ref"]): list(r["windows"]) for r in await store.list_charge_windows()}
    effective = resolve(rows, topology)
    return {
        "hub_id": hub_id,
        "bank_id": topology.bank_id,
        "tz": CHARGE_WINDOW_TZ,
        "windows": list(effective.windows) if effective else None,
        "source": {"scope_kind": effective.scope_kind, "scope_ref": effective.scope_ref}
        if effective
        else None,
    }


async def _propose(
    proposals: ProposalStore, store: StoreProtocol, identity: Identity, change: _Change
) -> dict[str, Any]:
    old = "none" if change.old_windows is None else ", ".join(change.old_windows) or "no charging"
    new = "remove" if change.new_windows is None else ", ".join(change.new_windows) or "no charging"
    summary = f"Charge windows {change.scope_kind} {change.scope_ref}: {old} -> {new} ({change.reason})"
    proposal = proposals.create(PROPOSAL_KIND, change, summary, identity.user)
    return {
        "proposal_id": str(proposal.proposal_id),
        "summary": summary,
        "scope_kind": change.scope_kind,
        "scope_ref": change.scope_ref,
        "old_windows": change.old_windows,
        "new_windows": change.new_windows,
        "expires_in_s": PROPOSAL_TTL_S,
    }


async def _checked_scope(store: StoreProtocol, scope_kind: str, scope_ref: str) -> str:
    try:
        kind = validate_scope(scope_kind, scope_ref)
    except ChargeWindowError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    if kind in ("HUB", "BANK"):
        known = await store.charge_window_topology(
            hub_id=scope_ref if kind == "HUB" else None, bank_id=scope_ref if kind == "BANK" else None
        )
        if known is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"{kind.lower()} {scope_ref!r} not found")
    return kind


@router.put("/{scope_kind}/{scope_ref}", status_code=status.HTTP_202_ACCEPTED)
async def propose_charge_windows(
    scope_kind: str,
    scope_ref: str,
    body: ChargeWindowPut,
    proposals: Annotated[ProposalStore, Depends(get_proposals)],
    store: Annotated[StoreProtocol, Depends(get_store)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    """Step 1: validate and propose. Windows are "HH:MM-HH:MM" America/Chicago, may wrap midnight, max 4;
    an empty list means no grid charging at that scope."""
    kind = await _checked_scope(store, scope_kind, scope_ref)
    try:
        windows = validate_windows(body.windows)
    except ChargeWindowError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    change = _Change(kind, scope_ref, await _current(store, kind, scope_ref), windows, body.reason)
    return await _propose(proposals, store, identity, change)


@router.delete("/{scope_kind}/{scope_ref}", status_code=status.HTTP_202_ACCEPTED)
async def propose_charge_window_removal(
    scope_kind: str,
    scope_ref: str,
    body: ChargeWindowDelete,
    proposals: Annotated[ProposalStore, Depends(get_proposals)],
    store: Annotated[StoreProtocol, Depends(get_store)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    """Step 1: propose removing an override (the next less specific scope then applies). The FLEET
    default cannot be removed."""
    kind = await _checked_scope(store, scope_kind, scope_ref)
    if kind == "FLEET":
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="the FLEET default cannot be removed; edit it instead"
        )
    old = await _current(store, kind, scope_ref)
    if old is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no override at that scope")
    return await _propose(proposals, store, identity, _Change(kind, scope_ref, old, None, body.reason))


@router.post("/proposals/{proposal_id}/confirm")
async def confirm_charge_windows(
    proposal_id: UUID,
    proposals: Annotated[ProposalStore, Depends(get_proposals)],
    trace_store: Annotated[TraceStore, Depends(get_trace_store)],
    store: Annotated[StoreProtocol, Depends(get_store)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    """Step 2: apply. 409 if the scope changed since the proposal was made."""
    try:
        proposal = proposals.pop(proposal_id, kind=PROPOSAL_KIND)
    except ProposalExpiredError as exc:
        raise HTTPException(status.HTTP_410_GONE, detail="proposal expired, propose again") from exc
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown proposal") from exc
    change: _Change = proposal.body
    if await _current(store, change.scope_kind, change.scope_ref) != change.old_windows:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="the windows changed since this was proposed")
    now = datetime.now(UTC)
    trace_ref = await trace_store.append(
        stream_id=f"operator_action:{identity.user}",
        decision_type="OPERATOR_ACTION",
        event_class=EVENT_CLASS,
        payload={
            "scope_kind": change.scope_kind,
            "scope_ref": change.scope_ref,
            "old_windows": change.old_windows,
            "new_windows": change.new_windows,
            "tz": CHARGE_WINDOW_TZ,
            "reason": change.reason,
            "proposal_id": str(proposal_id),
            "proposer": proposal.proposer,
        },
    )
    if change.new_windows is None:
        await store.delete_charge_window(change.scope_kind, change.scope_ref)
    else:
        await store.set_charge_window(
            change.scope_kind, change.scope_ref, change.new_windows, updated_by=identity.user
        )
    await store.insert_operator_action(
        operator_ref=identity.user,
        action_kind="CONFIG_CHANGE",
        target_ref=f"charge_window:{change.scope_kind}:{change.scope_ref}",
        tier="TIER1",
        reason=change.reason,
        trace_id=trace_ref.trace_id,
        confirmed_at=now,
    )
    return {
        "scope_kind": change.scope_kind,
        "scope_ref": change.scope_ref,
        "old_windows": change.old_windows,
        "windows": change.new_windows,
        "trace_id": str(trace_ref.trace_id),
    }
