"""Guardian check G-36 (R3.1): may the guardian sign this hub's FIRMWARE_UPDATE command?

A pure function over plain values, reusing `CheckOutcome` (no second verdict shape) and the catalogue rule
`opengrid.firmware.catalogue.hub_target_problem` (one copy, shared with campaign creation). The caller (the
guardian's firmware hand-off, `opengrid.firmware.guardian_flow`) reads every input itself from its own
sources -- never from the engine's plan -- and signs only on PASS (K3). The first failing rule is returned;
the order puts the hard-safety vetoes (stop, online, lease) before the scheduling ones (caps), so the traced
reason is the most important one.

Refusals are holds, never failures of the hub: the engine re-requests the job after its defer interval,
except the catalogue refusals (`opengrid.firmware.model.TERMINAL_REFUSALS`), which no waiting fixes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from opengrid.firmware.catalogue import Catalogue, hub_target_problem
from opengrid.firmware.model import concurrency_cap
from opengrid.guardian.checks import CheckOutcome

RULE_ID: Final = "G-36"

#: Campaign states in which an operator-requested per-hub ROLLBACK may still be signed (a halted
#: campaign is exactly when a rollback matters). UPDATE commands need RUNNING.
ROLLBACK_CAMPAIGN_STATES: Final = frozenset({"RUNNING", "PAUSED", "HALTED", "COMPLETED"})


@dataclass(frozen=True, slots=True)
class FirmwareHubFacts:
    """The guardian's own reads for one hub at evaluation time."""

    hub_id: str
    online: bool
    safe_stopped: bool
    soc_kwh: float | None
    reserve_kwh: float
    capacity_kwh: float
    already_updating: bool
    hardware_revision: str | None
    current_version: str | None
    committed_in_window: bool
    bank_in_flight: int
    bank_hub_count: int
    feeder_in_flight: int
    feeder_hub_count: int


@dataclass(frozen=True, slots=True)
class ProposedFirmwareCommand:
    hub_id: str
    action: str  # UPDATE | ROLLBACK
    target_version: str
    sha256: str
    hardware_revision: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class FirmwareCampaignGrant:
    """What the approved campaign allows (two-person approved when `override_committed` is set)."""

    campaign_state: str
    allow_downgrade: bool
    override_committed: bool
    override_second_operator: bool
    bank_max_concurrent_pct: float
    feeder_max_concurrent_pct: float


def check_firmware_eligibility(
    command: ProposedFirmwareCommand,
    hub: FirmwareHubFacts,
    grant: FirmwareCampaignGrant,
    catalogue: Catalogue,
    *,
    now: datetime,
    soc_margin_pct: float,
    max_lease_s: float,
    max_issue_skew_s: float,
) -> CheckOutcome:
    """G-36. Refuse to sign unless every rule holds:

    1. the campaign is RUNNING (a paused, halted or aborted campaign signs no UPDATE; an operator's
       per-hub ROLLBACK is still signed while it is paused, halted or completed, never once aborted);
    2. the lease is sane (not expired, not issued in the future beyond skew, no longer than `max_lease_s`);
    3. the hub is not inside an engaged safe stop, and is online;
    4. the hub is not already updating (one command in flight per hub);
    5. the hub serves no COMMITTED/DELIVERING obligation in the next update window, unless the campaign
       carries the committed-override AND that override was approved by a second operator;
    6. SoC >= reserve + `soc_margin_pct`% of capacity (an unknown SoC refuses);
    7. the (version, hardware revision) is catalogued, the sha256 matches the catalogue, and a downgrade
       is allowed (always for an operator ROLLBACK; otherwise only with the campaign's explicit flag);
    8. the bank and feeder concurrency caps are respected (counting this command).
    """
    hub_id = command.hub_id

    def refuse(reason: str) -> CheckOutcome:
        return CheckOutcome(RULE_ID, False, reason, hub_id)

    campaign_ok = grant.campaign_state == "RUNNING" or (
        command.action == "ROLLBACK" and grant.campaign_state in ROLLBACK_CAMPAIGN_STATES
    )
    if not campaign_ok:
        return refuse("FIRMWARE_CAMPAIGN_NOT_RUNNING")
    lease_s = (command.expires_at - command.issued_at).total_seconds()
    if (
        command.expires_at <= now
        or (command.issued_at - now).total_seconds() > max_issue_skew_s
        or lease_s <= 0.0
        or lease_s > max_lease_s
    ):
        return refuse("FIRMWARE_LEASE_INVALID")
    if hub.safe_stopped:
        return refuse("FIRMWARE_HUB_SAFE_STOPPED")
    if not hub.online:
        return refuse("FIRMWARE_HUB_OFFLINE")
    if hub.already_updating:
        return refuse("FIRMWARE_HUB_ALREADY_UPDATING")
    if hub.committed_in_window and not (grant.override_committed and grant.override_second_operator):
        return refuse("FIRMWARE_HUB_SERVES_COMMITTED_OBLIGATION")
    if hub.soc_kwh is None:
        return refuse("FIRMWARE_SOC_UNKNOWN")
    if hub.soc_kwh < hub.reserve_kwh + hub.capacity_kwh * soc_margin_pct / 100.0:
        return refuse("FIRMWARE_SOC_BELOW_RESERVE_MARGIN")

    if command.hardware_revision != hub.hardware_revision:
        return refuse("FIRMWARE_HARDWARE_REVISION_UNKNOWN")
    problem = hub_target_problem(
        catalogue,
        target_version=command.target_version,
        hardware_revision=hub.hardware_revision,
        current_version=hub.current_version,
        allow_downgrade=grant.allow_downgrade or command.action == "ROLLBACK",
    )
    if problem is not None:
        return refuse(problem)
    entry = catalogue.lookup(command.target_version, hub.hardware_revision)
    if entry is None or entry.sha256 != command.sha256:
        return refuse("FIRMWARE_SHA256_MISMATCH_CATALOGUE")

    if hub.bank_in_flight + 1 > concurrency_cap(hub.bank_hub_count, grant.bank_max_concurrent_pct):
        return refuse("FIRMWARE_BANK_CONCURRENCY_CAP")
    if hub.feeder_hub_count > 0 and hub.feeder_in_flight + 1 > concurrency_cap(
        hub.feeder_hub_count, grant.feeder_max_concurrent_pct
    ):
        return refuse("FIRMWARE_FEEDER_CONCURRENCY_CAP")
    return CheckOutcome.passed(RULE_ID, hub_id=hub_id)
