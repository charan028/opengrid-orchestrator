"""Cross-package sign -> verify contract (crypto.md S2.1): the hub's own verification code must accept
the batch the orchestrator's guardian signed, pinned in `interfaces/fixtures/signed_command_batch.json`
(written by `orchestrator/tests/unit/guardian/test_signed_batch_fixture.py`)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from ogsim.common.crypto import load_public_key
from ogsim.fleet.commands import evaluate_batch, parse_rfc3339, verify_batch_signature

FIXTURE = Path(__file__).resolve().parents[2] / "interfaces" / "fixtures" / "signed_command_batch.json"


def _load() -> tuple[dict, object]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data["batch"], load_public_key(data["guardian_public_key_hex"])


def test_hub_accepts_the_guardian_signed_batch():
    batch, public_key = _load()
    now = parse_rfc3339(batch["issued_at"])
    verdicts = evaluate_batch(batch, public_key, {}, now)
    assert verdicts and all(v.accepted for v in verdicts), verdicts


def test_hub_rejects_the_batch_if_a_setpoint_is_altered():
    batch, public_key = _load()
    tampered = copy.deepcopy(batch)
    tampered["items"][0]["p_kw_setpoint"] = -11.0
    assert verify_batch_signature(batch, public_key)
    assert not verify_batch_signature(tampered, public_key)
