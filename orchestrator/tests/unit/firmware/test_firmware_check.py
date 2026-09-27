"""G-36 `check_firmware_eligibility`: every veto, the committed-obligation override (two-person only), the
catalogue/downgrade rules, the lease, and the bank/feeder concurrency caps."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from opengrid.firmware.catalogue import Catalogue
from opengrid.guardian.firmware_check import (
    FirmwareCampaignGrant,
    FirmwareHubFacts,
    ProposedFirmwareCommand,
    check_firmware_eligibility,
)

from .fakes import CATALOGUE_CONFIG, SHA_142, SHA_150

NOW = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)
CATALOGUE = Catalogue.build(CATALOGUE_CONFIG)

HUB = FirmwareHubFacts(
    hub_id="hub-00001",
    online=True,
    safe_stopped=False,
    soc_kwh=30.0,
    reserve_kwh=7.84,
    capacity_kwh=39.2,
    already_updating=False,
    hardware_revision="revB",
    current_version="1.4.2",
    committed_in_window=False,
    bank_in_flight=0,
    bank_hub_count=20,
    feeder_in_flight=0,
    feeder_hub_count=40,
)
GRANT = FirmwareCampaignGrant(
    campaign_state="RUNNING",
    allow_downgrade=False,
    override_committed=False,
    override_second_operator=False,
    bank_max_concurrent_pct=10.0,
    feeder_max_concurrent_pct=10.0,
)
COMMAND = ProposedFirmwareCommand(
    hub_id="hub-00001",
    action="UPDATE",
    target_version="1.5.0",
    sha256=SHA_150,
    hardware_revision="revB",
    issued_at=NOW,
    expires_at=NOW + timedelta(seconds=120),
)


def _check(command=COMMAND, hub=HUB, grant=GRANT):
    return check_firmware_eligibility(
        command, hub, grant, CATALOGUE, now=NOW, soc_margin_pct=10.0, max_lease_s=120.0, max_issue_skew_s=5.0
    )


def test_eligible_hub_passes():
    outcome = _check()
    assert outcome.ok and outcome.rule_id == "G-36"


@pytest.mark.parametrize(
    ("hub_changes", "reason"),
    [
        ({"safe_stopped": True}, "FIRMWARE_HUB_SAFE_STOPPED"),
        ({"online": False}, "FIRMWARE_HUB_OFFLINE"),
        ({"already_updating": True}, "FIRMWARE_HUB_ALREADY_UPDATING"),
        ({"committed_in_window": True}, "FIRMWARE_HUB_SERVES_COMMITTED_OBLIGATION"),
        ({"soc_kwh": None}, "FIRMWARE_SOC_UNKNOWN"),
        ({"soc_kwh": 11.0}, "FIRMWARE_SOC_BELOW_RESERVE_MARGIN"),  # reserve 7.84 + 3.92 margin = 11.76
        ({"hardware_revision": "revA"}, "FIRMWARE_HARDWARE_REVISION_UNKNOWN"),
        ({"current_version": "1.5.0"}, "FIRMWARE_ALREADY_AT_TARGET"),
        ({"bank_in_flight": 2}, "FIRMWARE_BANK_CONCURRENCY_CAP"),  # 10% of 20 = 2
        ({"feeder_in_flight": 4, "bank_hub_count": 200}, "FIRMWARE_FEEDER_CONCURRENCY_CAP"),  # 10% of 40
    ],
)
def test_hub_vetoes(hub_changes, reason):
    outcome = _check(hub=replace(HUB, **hub_changes))
    assert not outcome.ok
    assert outcome.reason == reason
    assert outcome.hub_id == "hub-00001"


def test_soc_exactly_at_reserve_plus_margin_passes():
    assert _check(hub=replace(HUB, soc_kwh=7.84 + 3.92)).ok


def test_bank_cap_minimum_is_one():
    small = replace(HUB, bank_hub_count=3, feeder_hub_count=0)
    assert _check(hub=small).ok
    assert _check(hub=replace(small, bank_in_flight=1)).reason == "FIRMWARE_BANK_CONCURRENCY_CAP"


@pytest.mark.parametrize(
    "state", ["DRAFT", "PROPOSED", "APPROVED", "PAUSED", "HALTED", "COMPLETED", "ABORTED"]
)
def test_update_needs_a_running_campaign(state):
    assert _check(grant=replace(GRANT, campaign_state=state)).reason == "FIRMWARE_CAMPAIGN_NOT_RUNNING"


@pytest.mark.parametrize(("state", "ok"), [("HALTED", True), ("PAUSED", True), ("ABORTED", False)])
def test_rollback_is_signed_in_a_halted_campaign_never_an_aborted_one(state, ok):
    rollback = replace(COMMAND, action="ROLLBACK", target_version="1.4.2", sha256=SHA_142)
    hub = replace(HUB, current_version="1.5.0")
    assert _check(command=rollback, hub=hub, grant=replace(GRANT, campaign_state=state)).ok is ok


def test_committed_override_requires_the_second_operator():
    hub = replace(HUB, committed_in_window=True)
    single = replace(GRANT, override_committed=True, override_second_operator=False)
    assert _check(hub=hub, grant=single).reason == "FIRMWARE_HUB_SERVES_COMMITTED_OBLIGATION"
    double = replace(GRANT, override_committed=True, override_second_operator=True)
    assert _check(hub=hub, grant=double).ok


def test_version_not_in_catalogue_is_refused():
    command = replace(COMMAND, target_version="9.9.9")
    assert _check(command=command).reason == "FIRMWARE_NOT_IN_CATALOGUE_FOR_HARDWARE"


def test_sha256_must_match_the_catalogue():
    assert _check(command=replace(COMMAND, sha256="f" * 64)).reason == "FIRMWARE_SHA256_MISMATCH_CATALOGUE"


def test_command_hardware_revision_must_match_the_hub():
    assert (
        _check(command=replace(COMMAND, hardware_revision="revC")).reason
        == "FIRMWARE_HARDWARE_REVISION_UNKNOWN"
    )


def test_downgrade_needs_the_explicit_flag():
    command = replace(COMMAND, target_version="1.4.2", sha256=SHA_142)
    hub = replace(HUB, current_version="1.5.0")
    assert _check(command=command, hub=hub).reason == "FIRMWARE_DOWNGRADE_NOT_ALLOWED"
    assert _check(command=command, hub=hub, grant=replace(GRANT, allow_downgrade=True)).ok


@pytest.mark.parametrize(
    ("issued", "expires"),
    [
        (NOW - timedelta(seconds=200), NOW - timedelta(seconds=1)),  # expired
        (NOW + timedelta(seconds=30), NOW + timedelta(seconds=60)),  # issued in the future
        (NOW, NOW + timedelta(seconds=600)),  # lease too long
    ],
)
def test_lease_must_be_sane(issued, expires):
    command = replace(COMMAND, issued_at=issued, expires_at=expires)
    assert _check(command=command).reason == "FIRMWARE_LEASE_INVALID"


def test_safety_vetoes_take_precedence_over_scheduling():
    hub = replace(HUB, safe_stopped=True, bank_in_flight=99, soc_kwh=0.0)
    assert _check(hub=hub).reason == "FIRMWARE_HUB_SAFE_STOPPED"
