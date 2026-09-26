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
from opengrid.core.models.pq import CalibrationBounds as WireCalibrationBounds
from opengrid.core.models.pq import CalibrationCommand, CalibrationCorrection
from opengrid.core.pq import DEFAULT_FIRMWARE_CALIBRATION_BOUNDS, CalibrationBounds, OffsetVector
from opengrid.guardian import main as guardian_main
from opengrid.guardian.config import GuardianConfig
from opengrid.guardian.ports import ProposedBatch, ProposedItem
from opengrid.guardian.pq_ports import ProposedCalibrationCommand
from opengrid.guardian.repo import PendingCalibration
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


# --- S6.7 calibration hand-off ---------------------------------------------------------------------


def _pending(hub_id: str = "hub-1") -> PendingCalibration:
    return PendingCalibration(
        calibration_id=uuid4(),
        hub_id=hub_id,
        reference_phase_deg=0.0,
        reference_freq_hz=60.0,
        reference_amplitude_v=240.0,
        correction=OffsetVector(freq_hz=-0.02, voltage_pct=0.4, phase_deg=1.0),
        requested_at=NOW - timedelta(seconds=30),
    )


class _Queue:
    def __init__(self, rows: list[PendingCalibration]) -> None:
        self.rows = rows
        self.max_age_s: float | None = None

    async def pending(self, *, max_age_s: float) -> list[PendingCalibration]:
        self.max_age_s = max_age_s
        return list(self.rows)


class _Bounds:
    async def max_bounds_for_hub(self, hub_id: str) -> CalibrationBounds:
        return DEFAULT_FIRMWARE_CALIBRATION_BOUNDS


class _Signer:
    """Stands in for GuardianService: signs hub-1, refuses anything else."""

    def __init__(self) -> None:
        self.seen: list[ProposedCalibrationCommand] = []

    async def evaluate_and_sign_calibration(
        self, proposed: ProposedCalibrationCommand
    ) -> CalibrationCommand | None:
        self.seen.append(proposed)
        if proposed.hub_id != "hub-1":
            return None
        return CalibrationCommand(
            calibration_id=proposed.calibration_id,
            hub_id=proposed.hub_id,
            epoch=1,
            seq=1,
            issued_at=proposed.issued_at,
            expires_at=proposed.expires_at,
            reference=proposed.reference,
            correction=CalibrationCorrection(freq_hz=-0.02, voltage_pct=0.4, phase_deg=1.0),
            bounds=WireCalibrationBounds(max_freq_hz=0.1, max_voltage_pct=2.0, max_phase_deg=5.0),
            key_id="guardian-2026a",
            signature="sig",
        )


def test_build_proposed_calibration_uses_the_guardians_own_lease_bounds_and_time_source():
    pending = _pending()
    proposed = guardian_main.build_proposed_calibration(
        pending,
        bounds=DEFAULT_FIRMWARE_CALIBRATION_BOUNDS,
        now=NOW,
        lease_s=60.0,
        sync_source="ntp_disciplined",
    )
    assert proposed.calibration_id == pending.calibration_id and proposed.hub_id == "hub-1"
    assert proposed.issued_at == NOW and proposed.expires_at == NOW + timedelta(seconds=60)
    assert proposed.bounds == DEFAULT_FIRMWARE_CALIBRATION_BOUNDS
    assert proposed.reference.sync_source == "ntp_disciplined" and proposed.reference.freq_hz == 60.0
    assert proposed.correction == pending.correction


async def test_process_pending_calibrations_publishes_only_signed_commands():
    queue = _Queue([_pending("hub-1"), _pending("hub-2")])
    signer = _Signer()
    published: list[CalibrationCommand] = []

    async def publish(command: CalibrationCommand) -> None:
        published.append(command)

    count = await guardian_main.process_pending_calibrations(
        queue=queue,  # type: ignore[arg-type]
        firmware_bounds=_Bounds(),
        service=signer,  # type: ignore[arg-type]
        publish=publish,
        config=GuardianConfig(key_path=""),
        now_fn=lambda: NOW,
    )

    assert count == 1
    assert [c.hub_id for c in published] == ["hub-1"]
    assert [p.hub_id for p in signer.seen] == ["hub-1", "hub-2"]
    assert queue.max_age_s == GuardianConfig(key_path="").calibration_max_request_age_s


async def test_process_pending_calibrations_survives_a_publish_failure():
    async def failing_publish(command: CalibrationCommand) -> None:
        raise RuntimeError("broker down")

    count = await guardian_main.process_pending_calibrations(
        queue=_Queue([_pending("hub-1")]),  # type: ignore[arg-type]
        firmware_bounds=_Bounds(),
        service=_Signer(),  # type: ignore[arg-type]
        publish=failing_publish,
        config=GuardianConfig(key_path=""),
        now_fn=lambda: NOW,
    )
    assert count == 0


def test_main_publishes_the_evaluated_proposal_not_a_reread():
    """The PASS path publishes `service.evaluated_proposal(...)`; re-fetching the pre-image after the
    verdict could sign items the guardian never checked."""
    source = inspect.getsource(guardian_main.main)
    assert "evaluated_proposal" in source
    assert "proposals.fetch" not in source


def test_main_uses_process_names_for_mqtt_client_ids():
    source = inspect.getsource(guardian_main.main)
    assert 'process="guardian"' in source and "client_id=" not in source
