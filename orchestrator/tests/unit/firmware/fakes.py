"""In-memory `FirmwareRepo` for the firmware unit tests (API, campaigns, executor). Mirrors
`opengrid.firmware.repo.PgFirmwareRepo`'s semantics, including the command rows the guardian and the hub
ingest would update."""

from __future__ import annotations

import copy
from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import UUID

from opengrid.firmware.model import (
    Campaign,
    CampaignState,
    CommandRecord,
    Job,
    JobEvent,
    JobState,
)
from opengrid.firmware.planner import FleetCounts
from opengrid.firmware.repo import CommandRequest, HubRow, HubSelection

SHA_150 = "1" * 64
SHA_142 = "2" * 64
CATALOGUE_CONFIG = (
    {
        "version": "1.5.0",
        "hardware_revision": "revB",
        "sha256": SHA_150,
        "release_note": "Faster ramp; fixes",
    },
    {"version": "1.4.2", "hardware_revision": "revB", "sha256": SHA_142, "release_note": "Baseline"},
)


def fleet(
    banks: int = 2, per_bank: int = 10, *, version: str = "1.4.2", revision: str = "revB"
) -> list[HubRow]:
    rows = []
    for b in range(banks):
        for i in range(per_bank):
            rows.append(
                HubRow(
                    hub_id=f"hub-{b:02d}{i:03d}",
                    bank_id=f"bank-{b:02d}",
                    zone="NORTH",
                    feeder_id=f"feeder-{b:02d}",
                    firmware_version=version,
                    hardware_revision=revision,
                )
            )
    return rows


class FakeFirmwareRepo:
    def __init__(self, hubs: list[HubRow] | None = None, *, committed: Iterable[str] = ()) -> None:
        self.hubs = {h.hub_id: h for h in (hubs if hubs is not None else fleet())}
        self.committed = set(committed)
        self.catalogue_table: list[dict[str, Any]] = []
        self.campaigns: dict[UUID, Campaign] = {}
        self.job_rows: dict[UUID, Job] = {}
        self.event_rows: list[tuple[int, JobEvent]] = []
        self.command_rows: dict[UUID, CommandRecord] = {}
        self.command_requests: dict[UUID, CommandRequest] = {}

    # -- helpers for tests -------------------------------------------------------------------------
    def set_command(self, command_id: UUID, **changes: Any) -> None:
        self.command_rows[command_id] = replace(self.command_rows[command_id], **changes)

    def set_version(self, hub_id: str, version: str) -> None:
        self.hubs[hub_id] = replace(self.hubs[hub_id], firmware_version=version)

    def job_for(self, hub_id: str) -> Job:
        return next(j for j in self.job_rows.values() if j.hub_id == hub_id)

    # -- FirmwareRepo -------------------------------------------------------------------------------
    async def catalogue_rows(self) -> list[dict[str, Any]]:
        return list(self.catalogue_table)

    async def resolve_hubs(self, selection: HubSelection) -> list[HubRow]:
        if selection.is_empty():
            return []

        def ok(h: HubRow) -> bool:
            checks = (
                (selection.hub_ids, h.hub_id),
                (selection.bank_ids, h.bank_id),
                (selection.zones, h.zone),
                (selection.feeder_ids, h.feeder_id),
                (selection.hardware_revisions, h.hardware_revision),
                (selection.firmware_versions, h.firmware_version),
            )
            return all(not wanted or value in wanted for wanted, value in checks)

        return [h for h in sorted(self.hubs.values(), key=lambda h: h.hub_id) if ok(h)]

    async def committed_hub_ids(self, hub_ids: Sequence[str], *, until: datetime) -> set[str]:
        return {h for h in hub_ids if h in self.committed}

    async def fleet_counts(self) -> FleetCounts:
        bank: dict[str, int] = {}
        feeder: dict[str, int] = {}
        for h in self.hubs.values():
            bank[h.bank_id] = bank.get(h.bank_id, 0) + 1
            if h.feeder_id:
                feeder[h.feeder_id] = feeder.get(h.feeder_id, 0) + 1
        bank_flight: dict[str, int] = {}
        feeder_flight: dict[str, int] = {}
        for j in self.job_rows.values():
            campaign = self.campaigns[j.campaign_id]
            busy = j.state in (JobState.SENT, JobState.UPDATING) or (
                j.state == JobState.PENDING and j.command_id is not None
            )
            if busy and campaign.state != CampaignState.ABORTED:
                bank_flight[j.bank_id] = bank_flight.get(j.bank_id, 0) + 1
                if j.feeder_id:
                    feeder_flight[j.feeder_id] = feeder_flight.get(j.feeder_id, 0) + 1
        return FleetCounts(bank, feeder, bank_flight, feeder_flight)

    def _event(self, event: JobEvent) -> None:
        self.event_rows.append((len(self.event_rows) + 1, event))

    async def create_campaign(
        self, campaign: Campaign, jobs: Sequence[Job], events: Sequence[JobEvent]
    ) -> None:
        self.campaigns[campaign.campaign_id] = copy.deepcopy(campaign)
        for j in jobs:
            self.job_rows[j.job_id] = copy.deepcopy(j)
        for e in events:
            self._event(e)

    async def get_campaign(self, campaign_id: UUID) -> Campaign | None:
        c = self.campaigns.get(campaign_id)
        return copy.deepcopy(c) if c else None

    async def list_campaigns(self, *, limit: int = 50) -> list[Campaign]:
        ordered = sorted(self.campaigns.values(), key=lambda c: c.created_at, reverse=True)
        return [copy.deepcopy(c) for c in ordered[:limit]]

    async def active_campaigns(self) -> list[Campaign]:
        active = []
        for c in self.campaigns.values():
            open_jobs = any(
                j.campaign_id == c.campaign_id
                and j.state in (JobState.PENDING, JobState.SENT, JobState.UPDATING)
                for j in self.job_rows.values()
            )
            if c.state.value in ("APPROVED", "RUNNING", "PAUSED", "HALTED") or (
                c.state == CampaignState.COMPLETED and open_jobs
            ):
                active.append(copy.deepcopy(c))
        return active

    async def jobs(self, campaign_id: UUID) -> list[Job]:
        rows = [j for j in self.job_rows.values() if j.campaign_id == campaign_id]
        return [copy.deepcopy(j) for j in sorted(rows, key=lambda j: (j.wave, j.hub_id))]

    async def events(
        self, campaign_id: UUID, *, after_id: int = 0, limit: int = 500
    ) -> list[tuple[int, JobEvent]]:
        rows = [(i, e) for i, e in self.event_rows if e.campaign_id == campaign_id and i > after_id]
        return rows[:limit]

    async def save_campaign(self, campaign: Campaign, events: Sequence[JobEvent]) -> None:
        self.campaigns[campaign.campaign_id] = copy.deepcopy(campaign)
        for e in events:
            self._event(e)

    async def save_jobs(self, jobs: Sequence[Job], events: Sequence[JobEvent]) -> None:
        for j in jobs:
            self.job_rows[j.job_id] = copy.deepcopy(j)
        for e in events:
            self._event(e)

    async def request_commands(self, requests: Sequence[CommandRequest]) -> None:
        for r in requests:
            self.command_requests[r.command_id] = r
            self.command_rows[r.command_id] = CommandRecord(
                command_id=r.command_id,
                job_id=r.job.job_id,
                hub_id=r.job.hub_id,
                attempt=r.job.attempts + 1,
                status="REQUESTED",
                expires_at=r.expires_at,
            )
            stored = self.job_rows[r.job.job_id]
            stored.command_id = r.command_id

    async def commands(self, command_ids: Iterable[UUID]) -> dict[UUID, CommandRecord]:
        return {i: self.command_rows[i] for i in command_ids if i in self.command_rows}

    async def hub_versions(self, hub_ids: Iterable[str]) -> dict[str, str | None]:
        return {h: self.hubs[h].firmware_version for h in hub_ids if h in self.hubs}
