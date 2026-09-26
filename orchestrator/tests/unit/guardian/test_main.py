"""Pure(ish) helpers in `opengrid.guardian.main`, against the same fake psycopg pool/cursor used in
`test_repo.py` and the fake MQTT client from `test_mqtt_io.py` -- no real DB/MQTT connection."""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from opengrid.core.crypto import generate_keypair, verify_payload
from opengrid.core.models.engine import Verdict
from opengrid.core.models.mqtt import COMMAND_BATCH_SIGNED_FIELDS
from opengrid.guardian import main as guardian_main
from opengrid.guardian.ports import ProposedBatch, ProposedItem
from opengrid.guardian.service import GuardianService
from opengrid.platform.config import Config

from .test_mqtt_io import FakePublishClient
from .test_repo import FakeCursor, FakePool

NOW = datetime(2026, 9, 26, 18, 0, 0, tzinfo=UTC)


def test_main_wires_mqtt_hub_state_port_not_postgres():
    """GUARD-02/04: og-guardian's hub-state read must be its OWN MQTT telemetry cache
    (`opengrid.guardian.mqtt_io.MqttHubStatePort`), never a Postgres-backed port reading `og.hub_state`
    (the row the engine/fleet processes maintain) -- a batch the allocator planned around could then be
    signed off a value nobody independently verified."""
    from opengrid.guardian.mqtt_io import MqttHubStatePort

    source = inspect.getsource(guardian_main)
    assert "MqttHubStatePort" in source
    assert "PgHubStatePort" not in source
    assert guardian_main.MqttHubStatePort is MqttHubStatePort


async def test_fetch_pending_batches_maps_rows():
    command_batch_id = uuid4()
    trace_pre_image_id = uuid4()
    cursor = FakeCursor([[(command_batch_id, "cycle-1", 3, "sub-1", 1, "root-hash", trace_pre_image_id)]])
    rows = await guardian_main._fetch_pending_batches(FakePool(cursor))
    assert len(rows) == 1
    assert rows[0].command_batch_id == command_batch_id
    assert rows[0].ledger_version == 3


async def test_insert_verdict_commits():
    cursor = FakeCursor([None])
    pool = FakePool(cursor)
    verdict = Verdict(
        verdict_id=uuid4(),
        command_batch_id=uuid4(),
        outcome="PASS",
        vetoed_rule_ids=[],
        latency_ms=5,
        inputs_hash="a" * 64,
        signature="sig",
        signed_at=NOW,
    )
    await guardian_main._insert_verdict(pool, verdict)
    conn = pool.connection()
    assert conn.committed is True
    assert cursor.executed[0][1]["command_batch_id"] == verdict.command_batch_id


def _proposal() -> ProposedBatch:
    return ProposedBatch(
        command_batch_id=uuid4(),
        bank_id="bank-1",
        cycle_id="cycle-1",
        epoch=1,
        seq=1,
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=10),
        ledger_version=1,
        items=[
            ProposedItem(hub_id="hub-1", p_kw_setpoint=3.0, reason_code="SELECTOR"),
            ProposedItem(hub_id="hub-2", p_kw_setpoint=-2.0, reason_code="SELECTOR"),
        ],
    )


def _service(seed: bytes) -> GuardianService:
    return GuardianService(ports=None, config=None, signing_seed=seed)  # type: ignore[arg-type]


def test_built_batch_is_signed_over_its_own_envelope_fields():
    """crypto.md S2.1: the published signature must verify over the envelope's 7 signed fields --
    the hub's check. The verdict's signature (S2.2, different fields) must never be reused here."""
    seed, public = generate_keypair()
    batch = guardian_main.build_signed_batch(_service(seed), key_id="guardian-2026a", proposal=_proposal())

    wire = json.loads(batch.model_dump_json())
    signed_fields = {k: wire[k] for k in COMMAND_BATCH_SIGNED_FIELDS}
    assert verify_payload(public, signed_fields, wire["signature"])
    assert wire["key_id"] == "guardian-2026a"


async def test_publish_signed_batch_publishes_batch_and_leases_per_item():
    client = FakePublishClient()
    proposal = _proposal()
    seed, _public = generate_keypair()
    cfg = Config({"mqtt": {"topic_root": "ogtest/guard"}})

    await guardian_main._publish_signed_batch(
        mqtt_client=client, cfg=cfg, service=_service(seed), key_id="guardian-2026a", proposal=proposal
    )

    topics = [p[0] for p in client.published]
    assert "ogtest/guard/cmd/bank-1/batch" in topics
    assert "ogtest/guard/lease/hub-1" in topics
    assert "ogtest/guard/lease/hub-2" in topics
    assert len(client.published) == 3
