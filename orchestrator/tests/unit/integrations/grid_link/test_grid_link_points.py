"""Point mapping "opengrid-gridlink-v1": the orchestrator's constants equal the interfaces contract, and
roles and status values map onto the right indices."""

from __future__ import annotations

import json
from pathlib import Path

from opengrid.integrations.grid_link import points as p
from opengrid.integrations.grid_link.config import MAX_BANKS, MAX_TARGETS
from opengrid.integrations.grid_link.dnp3_session import CONTROL_STATUS
from opengrid.integrations.grid_link.model import (
    CALL_REASON_CODES,
    BankStatus,
    CallPhase,
    ControlVerdict,
    LinkStatus,
    TargetStatus,
)

CONTRACT = Path(__file__).resolve().parents[5] / "interfaces" / "grid_link" / "opengrid-gridlink-v1.json"


def test_ts_gl_points_match_the_interfaces_contract() -> None:
    spec = json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert spec["analog_outputs"]["fixed"] == p.AO_FIXED
    assert spec["analog_outputs"]["per_target"]["base"] == p.AO_TARGET_BASE
    assert spec["binary_outputs"]["fixed"] == p.BO_FIXED
    assert spec["binary_outputs"]["per_target"]["base"] == p.BO_TARGET_BASE
    assert spec["analog_inputs"]["fixed"] == p.AI_FIXED
    assert spec["analog_inputs"]["per_target"]["base"] == p.AI_TARGET_BASE
    assert spec["analog_inputs"]["per_bank"]["base"] == p.AI_BANK_BASE
    assert spec["analog_inputs"]["per_bank"]["stride"] == p.AI_BANK_STRIDE
    assert spec["binary_inputs"]["fixed"] == p.BI_FIXED
    assert spec["binary_inputs"]["per_target"]["base"] == p.BI_TARGET_BASE
    assert spec["call_state_codes"] == {phase.name: int(phase) for phase in CallPhase}
    assert {
        k: v for k, v in spec["call_reason_codes"].items() if k not in ("NONE", "UNKNOWN")
    } == CALL_REASON_CODES
    by_name = spec["control_status_codes"]
    names = {
        ControlVerdict.ACCEPTED: "SUCCESS",
        ControlVerdict.TIMEOUT: "TIMEOUT",
        ControlVerdict.FORMAT_ERROR: "FORMAT_ERROR",
        ControlVerdict.NOT_SUPPORTED: "NOT_SUPPORTED",
        ControlVerdict.NOT_AUTHORIZED: "NOT_AUTHORIZED",
        ControlVerdict.INHIBITED: "AUTOMATION_INHIBIT",
        ControlVerdict.OUT_OF_RANGE: "OUT_OF_RANGE",
    }
    assert {verdict: by_name[name] for verdict, name in names.items()} == CONTROL_STATUS
    assert spec["limits"] == {"max_targets": MAX_TARGETS, "max_banks": MAX_BANKS, "no_l2_ceiling": -1}
    # the per-target and per-bank regions never overlap at the configured maxima
    assert p.AI_TARGET_BASE + MAX_TARGETS <= p.AI_BANK_BASE


def test_control_roles_are_the_allow_list() -> None:
    points = p.GridLinkPointMap(["LZ_AEN", "B041"], ["bank-040"])
    assert points.analog_output_role(0) == p.ControlRole("CALL_SETPOINT_KW")
    assert points.analog_output_role(17) == p.ControlRole("L2_LIMIT_KW", "B041")
    assert points.analog_output_role(18) is None  # third target does not exist
    assert points.analog_output_role(3) is None
    assert points.binary_output_role(2) == p.ControlRole("HEARTBEAT")
    assert points.binary_output_role(16) == p.ControlRole("L2_LIMIT_ACTIVE", "LZ_AEN")
    assert points.binary_output_role(19) == p.ControlRole("L2_BLOCK", "B041")
    assert points.binary_output_role(20) is None


def test_status_encodes_onto_input_points() -> None:
    points = p.GridLinkPointMap(["LZ_AEN", "B041"], ["bank-040", "bank-041"])
    status = LinkStatus(
        available_kw=1800.0,
        delivered_kw=600.0,
        call_phase=CallPhase.ACTIVE,
        ems_call_id=42,
        call_reason=0,
        call_delivered_kw=590.0,
        soc_pct=61.5,
        heartbeat_count=12,
        link_healthy=True,
        telemetry_stale=True,
        toll_calls_enabled=True,
        banks=(BankStatus("bank-040", 60.0, 900.0, 300.0, l2_ceiling_kw=250.0),),
        targets=(TargetStatus("LZ_AEN", False, True, None), TargetStatus("B041", True, False, 250.0)),
    )
    analogs = points.analog_inputs(status)
    assert analogs[0] == 1800.0 and analogs[2] == 2.0 and analogs[3] == 42.0 and analogs[6] == 61.5
    assert analogs[16] == p.NO_L2_CEILING and analogs[17] == 250.0
    assert [analogs[100 + k] for k in range(4)] == [60.0, 900.0, 300.0, 250.0]
    assert 104 not in analogs  # no telemetry for bank-041: served as COMM_LOST, never invented
    binaries = points.binary_inputs(status)
    assert binaries[0] and binaries[1] and not binaries[2] and binaries[4] and binaries[5]
    assert binaries[16] is False and binaries[17] is True and binaries[18] is True and binaries[19] is False
    assert len(points.analog_input_indices()) == 8 + 2 + 2 * 4
