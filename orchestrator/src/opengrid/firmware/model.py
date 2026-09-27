"""Firmware campaign data model (R3.1): states, the catalogue entry, campaign and job records, the timestamped
event, and the guardian-signed `FirmwareCommand` wire model (`interfaces/mqtt/firmware_command.schema.json`).

Pure data, no I/O. The allowed state transitions live here (one table per record kind) so the API, the
planner and the executor all refuse the same illegal moves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class CampaignState(StrEnum):
    DRAFT = "DRAFT"
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    HALTED = "HALTED"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"


class JobState(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    UPDATING = "UPDATING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    SKIPPED = "SKIPPED"


JobAction = Literal["UPDATE", "ROLLBACK"]

#: Campaign transitions an operator or the executor may make. HALTED is left only by abort or by the
#: operator's "retry failed hubs" (-> PAUSED), never straight back to RUNNING.
CAMPAIGN_TRANSITIONS: Final[dict[CampaignState, frozenset[CampaignState]]] = {
    CampaignState.DRAFT: frozenset({CampaignState.PROPOSED, CampaignState.APPROVED, CampaignState.ABORTED}),
    CampaignState.PROPOSED: frozenset({CampaignState.APPROVED, CampaignState.ABORTED}),
    CampaignState.APPROVED: frozenset({CampaignState.RUNNING, CampaignState.ABORTED}),
    CampaignState.RUNNING: frozenset(
        {CampaignState.PAUSED, CampaignState.HALTED, CampaignState.COMPLETED, CampaignState.ABORTED}
    ),
    CampaignState.PAUSED: frozenset({CampaignState.RUNNING, CampaignState.ABORTED}),
    CampaignState.HALTED: frozenset({CampaignState.PAUSED, CampaignState.ABORTED}),
    CampaignState.COMPLETED: frozenset({CampaignState.RUNNING}),  # only via "retry failed hubs"
    CampaignState.ABORTED: frozenset(),
}

#: Terminal job states (a wave is finished when every job in it is terminal).
TERMINAL_JOB_STATES: Final = frozenset(
    {JobState.SUCCEEDED, JobState.FAILED, JobState.ROLLED_BACK, JobState.SKIPPED}
)
IN_FLIGHT_JOB_STATES: Final = frozenset({JobState.SENT, JobState.UPDATING})

#: Guardian (G-36) refusals that end the job instead of deferring it: no amount of waiting fixes them.
TERMINAL_REFUSALS: Final = frozenset(
    {
        "FIRMWARE_HARDWARE_REVISION_UNKNOWN",
        "FIRMWARE_NOT_IN_CATALOGUE_FOR_HARDWARE",
        "FIRMWARE_SHA256_MISMATCH_CATALOGUE",
        "FIRMWARE_DOWNGRADE_NOT_ALLOWED",
    }
)
#: A refusal meaning the hub already runs the target: the job is done, not failed.
ALREADY_AT_TARGET: Final = "FIRMWARE_ALREADY_AT_TARGET"

#: Hub-reported failures (firmware_status.reason) and orchestrator-detected ones that are worth a retry.
TRANSIENT_FAILURES: Final = frozenset(
    {
        "NO_ACK",
        "EXPIRED",
        "UPDATE_TIMEOUT",
        "HUB_OFFLINE",
        "DOWNLOAD_ERROR",
        "VERIFY_ERROR",
        "ALREADY_UPDATING",
    }
)
#: Never retried; terminal and alerted. Anything not listed in either set is treated as terminal too.
TERMINAL_FAILURES: Final = frozenset(
    {"HASH_MISMATCH", "HARDWARE_INCOMPATIBLE", "BAD_SIGNATURE", "INSTALL_ERROR", "BOOT_FAILED", "UNKNOWN_HUB"}
)

_VERSION_RE: Final = re.compile(r"^[0-9]+(\.[0-9]+){1,3}(-[0-9A-Za-z.]+)?$")
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")


def is_valid_version(version: str) -> bool:
    return bool(_VERSION_RE.match(version))


def is_valid_sha256(value: str) -> bool:
    return bool(_SHA256_RE.match(value))


def version_key(version: str) -> tuple[tuple[int, ...], int, str]:
    """Sort key: numeric release parts, then a release (no suffix) above any pre-release of it."""
    release, _, suffix = version.partition("-")
    parts = tuple(int(p) for p in release.split("."))
    parts = parts + (0,) * (4 - len(parts))
    return parts, (0 if suffix else 1), suffix


def concurrency_cap(hub_count: int, pct: float) -> int:
    """Max hubs updating at once in a bank/feeder: `pct`% of its hubs, rounded down, at least 1. The one
    copy: the planner schedules to it and the guardian (G-36) enforces it."""
    return max(1, int(hub_count * pct / 100.0))


def is_downgrade(current: str | None, target: str) -> bool:
    """True when `target` is older than `current`. An unknown or unparseable current version is never a
    downgrade (the catalogue check still applies)."""
    if not current or not is_valid_version(current):
        return False
    return version_key(target) < version_key(current)


@dataclass(frozen=True, slots=True)
class CatalogueEntry:
    version: str
    hardware_revision: str
    sha256: str
    release_note: str
    released_at: str | None = None
    source: Literal["config", "table"] = "config"


@dataclass(frozen=True, slots=True)
class WaveSpec:
    """Canary then waves. Each size is a hub count or, when `*_pct` is set, a percentage of the campaign."""

    canary: int = 1
    canary_pct: float | None = None
    sizes: tuple[int, ...] = ()
    size_pct: float | None = 25.0


@dataclass(slots=True)
class Campaign:
    campaign_id: UUID
    name: str
    target_version: str
    state: CampaignState
    hub_ids: list[str]
    waves: list[int]
    reason: str
    proposed_by: str
    created_at: datetime
    selection: dict[str, Any] = field(default_factory=dict)
    bank_max_concurrent_pct: float = 10.0
    feeder_max_concurrent_pct: float = 10.0
    max_failures: int = 2
    max_failure_pct: float = 5.0
    window_start: datetime | None = None
    window_end: datetime | None = None
    allow_downgrade: bool = False
    override_committed: bool = False
    requires_second_operator: bool = False
    second_operator_reasons: list[str] = field(default_factory=list)
    confirmed_by: str | None = None
    approved_by: str | None = None
    halt_reason: str | None = None
    confirmed_at: datetime | None = None
    approved_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    trace_id: UUID | None = None


@dataclass(slots=True)
class Job:
    job_id: UUID
    campaign_id: UUID
    hub_id: str
    bank_id: str
    wave: int
    target_version: str
    state: JobState = JobState.PENDING
    feeder_id: str | None = None
    action: JobAction = "UPDATE"
    from_version: str | None = None
    attempts: int = 0
    next_attempt_at: datetime | None = None
    command_id: UUID | None = None
    reason: str | None = None
    terminal_failure: bool = False
    sent_at: datetime | None = None
    updating_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class JobEvent:
    """One timestamped transition (owner addition 1). `job_id`/`hub_id` are None for a campaign event."""

    campaign_id: UUID
    event: str
    ts: datetime
    job_id: UUID | None = None
    hub_id: str | None = None
    from_state: str | None = None
    to_state: str | None = None
    reason: str | None = None
    attempt: int | None = None
    detail: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class CommandRecord:
    """The engine's view of one `og.firmware_command` row (the current attempt of a job)."""

    command_id: UUID
    job_id: UUID
    hub_id: str
    attempt: int
    status: str  # REQUESTED | RESERVED | SIGNED | REFUSED | PUBLISHED
    expires_at: datetime
    refuse_reason: str | None = None
    hub_state: str | None = None
    hub_reason: str | None = None
    hub_version: str | None = None
    hub_ts: datetime | None = None


@dataclass(frozen=True, slots=True)
class HubView:
    """What the planner needs about a hub (read by the executor from og.hub/og.bank/og.hub_state)."""

    hub_id: str
    bank_id: str
    feeder_id: str | None
    firmware_version: str | None
    hardware_revision: str | None


class FirmwareCommand(BaseModel):
    """Guardian -> hub, `<root>/cmd/fw/<hub_id>`, QoS 1, guardian-signed (K3). Mirrors
    `firmware_command.schema.json`; the signature covers every field except key_id/signature."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: UUID
    campaign_id: UUID
    job_id: UUID
    hub_id: str
    action: JobAction
    target_version: str
    from_version: str | None = None
    sha256: str
    hardware_revision: str
    attempt: int
    epoch: int
    seq: int
    issued_at: datetime
    expires_at: datetime
    key_id: str
    signature: str

    def signing_payload(self) -> dict[str, Any]:
        """Fields covered by the Ed25519 signature (excludes key_id/signature), per interfaces/crypto.md."""
        data: dict[str, Any] = self.model_dump(mode="json", exclude={"key_id", "signature"})
        return data
