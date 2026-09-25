"""Tests for ogsim.common.scenario -- tolerant <root>/scenario/cmd parsing
for both the schema-conformant shape and ogsim.control's current legacy
flat shape (BUILD.md point 4's documented wire-shape compromise)."""

from __future__ import annotations

from ogsim.common.scenario import parse_scenario_cmd


def test_parses_schema_conformant_shape() -> None:
    raw = {
        "id": "11111111-1111-1111-1111-111111111111",
        "target": {"kind": "hub", "ref": "hub-0007"},
        "type": "FLEET_HUB_OFFLINE",
        "params": {},
        "start": "2026-09-26T18:00:00.000Z",
        "duration_s": 60,
    }
    cmd = parse_scenario_cmd(raw)
    assert cmd.catalogue_type == "hub_offline"
    assert cmd.target_kind == "hub"
    assert cmd.target_ref == "hub-0007"
    assert cmd.duration_s == 60


def test_parses_legacy_flat_shape() -> None:
    raw = {
        "id": "22222222-2222-2222-2222-222222222222",
        "target": "hub-0007",
        "type": "hub_offline",
        "params": {},
        "start": 1780000000.0,
        "duration": 60.0,
    }
    cmd = parse_scenario_cmd(raw)
    assert cmd.catalogue_type == "hub_offline"
    assert cmd.target_kind is None
    assert cmd.target_ref == "hub-0007"
    assert cmd.duration_s == 60.0
    assert cmd.start_epoch == 1780000000.0


def test_scada_bad_quality_maps_to_full_catalogue_id() -> None:
    raw = {
        "id": "33333333-3333-3333-3333-333333333333",
        "target": {"kind": "bank", "ref": "bank-003"},
        "type": "SCADA_BAD_QUALITY",
        "params": {},
        "start": "2026-09-26T18:00:00.000Z",
    }
    cmd = parse_scenario_cmd(raw)
    assert cmd.catalogue_type == "bad_quality_flag"
    assert cmd.duration_s is None
