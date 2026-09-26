"""Cross-package sign -> verify contract for calibration commands (S6.7): the hub's own verification
code must accept the command the orchestrator's guardian signed, pinned in
`interfaces/fixtures/signed_calibration_command.json` (written by
`orchestrator/tests/unit/guardian/test_signed_calibration_fixture.py`)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from ogsim.common.crypto import load_public_key
from ogsim.fleet.calibration import verify_calibration_signature

FIXTURE = Path(__file__).resolve().parents[2] / "interfaces" / "fixtures" / "signed_calibration_command.json"


def _load() -> tuple[dict, object]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data["command"], load_public_key(data["guardian_public_key_hex"])


def test_hub_accepts_the_guardian_signed_calibration_command():
    command, public_key = _load()
    assert verify_calibration_signature(command, public_key)


def test_hub_rejects_the_command_if_any_signed_field_is_altered():
    command, public_key = _load()
    for field, value in (
        ("seq", command["seq"] + 1),
        ("hub_id", "hub-00002"),
        ("expires_at", command["issued_at"]),
    ):
        tampered = copy.deepcopy(command)
        tampered[field] = value
        assert not verify_calibration_signature(tampered, public_key), field
    tampered = copy.deepcopy(command)
    tampered["correction"]["phase_deg"] = 4.9
    assert not verify_calibration_signature(tampered, public_key)
