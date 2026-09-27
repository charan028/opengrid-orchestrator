"""Protocol loopback: the ogsim EMS master and the ogsim SCADA grid-link bridge against OpenGrid's grid-link
outstation, over real DNP3 on localhost TCP and mutual TLS (OS-assigned ports >= 18000; no broker, no DB).
Covers the toll-call path, L2 from the SCADA sim, the heartbeat fail-safe, and allow-list / CN refusals."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from opengrid.integrations.grid_link.dnp3_server import Dnp3GridLinkServer
from opengrid.integrations.grid_link.model import CallPhase
from opengrid.integrations.tls import ServerTlsSettings

from .certs import Pki
from .fakes import Harness, make_harness, make_settings

master_mod = pytest.importorskip("ogsim.protocols.dnp3_master")
points_mod = pytest.importorskip("ogsim.protocols.gridlink_points")
bridge_mod = pytest.importorskip("ogsim.scada.grid_link")

AI, BI, BO, AO = points_mod.AI, points_mod.BI, points_mod.BO, points_mod.AO
STATUS = points_mod.STATUS


async def _start(h: Harness) -> tuple[Dnp3GridLinkServer, int]:
    server = Dnp3GridLinkServer(h.service.settings, h.service, monotonic=h.clock)
    _, port = await server.start()
    assert port >= 18000
    return server, port


@pytest.fixture
async def link() -> AsyncIterator[tuple[Harness, Any]]:
    h = make_harness()
    server, port = await _start(h)
    master = master_mod.Dnp3Master("127.0.0.1", port, timeout_s=3.0)
    await master.connect()
    try:
        yield h, master
    finally:
        await master.close()
        await server.stop()


async def _stage_and_execute(master: Any, *, kw: int, minutes: int, call_id: int) -> int:
    for index, value in (
        (AO["CALL_SETPOINT_KW"], kw),
        (AO["CALL_DURATION_MIN"], minutes),
        (AO["CALL_ID"], call_id),
    ):
        assert await master.analog_output(index, value) == STATUS["SUCCESS"]
    return int(await master.crob(BO["CALL_EXECUTE"], points_mod.CROB_LATCH_ON, sbo=True))


async def test_ts_gl_e2e_toll_call_over_dnp3_reaches_the_core_and_reports_back(
    link: tuple[Harness, Any],
) -> None:
    h, master = link
    assert await master.crob(BO["HEARTBEAT"], points_mod.CROB_PULSE_ON) == STATUS["SUCCESS"]
    assert await _stage_and_execute(master, kw=1500, minutes=90, call_id=4242) == STATUS["SUCCESS"]
    await h.service.drain()
    await h.service.tick()
    assert h.calls.issued == [("AUSTIN_ENERGY", 4242, 1500.0, 90)]
    assert h.trace.events("GRID_LINK_COMMAND")[0]["origin"] == "GRID_LINK"

    values = await master.integrity_poll()
    assert values.analogs[AI["CALL_STATE"]] == float(CallPhase.ACCEPTED)
    assert values.analogs[AI["CALL_ID"]] == 4242.0
    assert values.analogs[AI["AVAILABLE_KW"]] == 3000.0 and values.analogs[AI["SOC_PCT"]] == 50.0
    assert values.binaries[BI["LINK_HEALTHY"]] and values.binaries[BI["CALL_ACTIVE"]]

    assert await master.analog_output(AO["CALL_ID"], 4242) == STATUS["SUCCESS"]
    assert await master.crob(BO["CALL_CANCEL"], points_mod.CROB_PULSE_ON, sbo=True) == STATUS["SUCCESS"]
    await h.service.drain()
    assert h.calls.cancelled == [("AUSTIN_ENERGY", 4242)]
    values = await master.integrity_poll()
    assert values.analogs[AI["CALL_STATE"]] == float(CallPhase.ENDED)


async def test_ts_gl_heartbeat_loss_fails_safe_over_the_wire(link: tuple[Harness, Any]) -> None:
    h, master = link
    await master.crob(BO["HEARTBEAT"], points_mod.CROB_PULSE_ON)
    await master.analog_output(points_mod.AO_TARGET_BASE + 1, 300)
    await master.crob(points_mod.BO_TARGET_BASE + 2, points_mod.CROB_LATCH_ON, sbo=True)  # B041 LIMIT on
    await h.service.drain()
    h.clock.advance(h.service.settings.heartbeat_timeout_s + 1)
    await h.service.tick()

    status = await _stage_and_execute(master, kw=100, minutes=10, call_id=1)
    assert status == STATUS["AUTOMATION_INHIBIT"]
    values = await master.integrity_poll()
    assert not values.binaries[BI["LINK_HEALTHY"]] and values.binaries[BI["ALARM_HEARTBEAT_LOST"]]
    assert values.analogs[points_mod.AI_TARGET_BASE + 1] == 300.0  # L2 limit kept as last known
    assert [i.kind for i in h.sink.instructions] == ["LIMIT"]  # never lifted by the loss
    assert h.calls.issued == []


async def test_ts_gl_scada_sim_sends_l2_limit_and_block_over_the_link(link: tuple[Harness, Any]) -> None:
    h, master = link
    settings = bridge_mod.GridLinkBridgeSettings(targets={"bank-041": 1})
    bridge = bridge_mod.ScadaGridLinkBridge(settings, master=master)
    issued = "2026-09-26T20:00:00Z"
    assert await bridge.heartbeat() == STATUS["SUCCESS"]
    limit = {
        "bank_id": "bank-041",
        "kind": "LIMIT",
        "limit_kw": 420.0,
        "issued_at": issued,
        "expires_at": None,
    }
    block = {
        "bank_id": "bank-041",
        "kind": "BLOCK",
        "limit_kw": None,
        "issued_at": issued,
        "expires_at": None,
    }
    assert await bridge.send(limit) == [0, 0]
    assert await bridge.send(block) == [0]
    await h.service.drain()
    kinds = [(i.bank_id, i.kind, i.limit_kw) for i in h.sink.instructions]
    assert kinds == [("bank-041", "LIMIT", 420.0), ("bank-041", "BLOCK", None)]
    assert await bridge.send({**block, "expires_at": issued}) == [0]  # lift
    await h.service.drain()
    assert h.sink.instructions[-1].kind == "LIMIT"  # the LIMIT level is still on underneath


async def test_ts_gl_peer_outside_the_allow_list_is_refused_and_traced() -> None:
    h = make_harness(make_settings(allowed_peers=["10.0.0.0/8"]))
    server, port = await _start(h)
    master = master_mod.Dnp3Master("127.0.0.1", port, timeout_s=1.0)
    try:
        await master.connect()
        with pytest.raises(master_mod.Dnp3MasterError):
            await master.integrity_poll()
    finally:
        await master.close()
        await server.stop()
    await h.service.drain()
    denies = h.trace.events("GRID_LINK_DENY")
    assert denies and denies[0]["reason"] == "peer-not-allowed" and denies[0]["origin"] == "GRID_LINK"


async def test_ts_gl_mutual_tls_admits_only_allowed_client_cns(tmp_path: Path) -> None:
    pki = Pki(tmp_path)
    server_pem, good, rogue = (
        pki.issue("og-gridlink", server=True),
        pki.issue("aen-ems-1"),
        pki.issue("rogue"),
    )
    tls = ServerTlsSettings(
        enabled=True, cert_file=server_pem.cert, key_file=server_pem.key, client_ca_file=pki.ca.cert
    )
    h = make_harness(make_settings(tls=tls, allowed_peer_cns=["aen-ems-1"]))
    server, port = await _start(h)
    try:
        ok = master_mod.Dnp3Master(
            "127.0.0.1",
            port,
            ssl_context=master_mod.client_ssl_context(pki.ca.cert, good.cert, good.key),
            timeout_s=2.0,
        )
        await ok.connect()
        assert (await ok.integrity_poll()).analogs[AI["AVAILABLE_KW"]] == 0.0
        await ok.close()

        bad = master_mod.Dnp3Master(
            "127.0.0.1",
            port,
            ssl_context=master_mod.client_ssl_context(pki.ca.cert, rogue.cert, rogue.key),
            timeout_s=2.0,
        )
        await bad.connect()
        with pytest.raises(master_mod.Dnp3MasterError):
            await bad.integrity_poll()
        await bad.close()
    finally:
        await server.stop()
    await h.service.drain()
    assert [d["reason"] for d in h.trace.events("GRID_LINK_DENY")] == ["peer-cn-not-allowed"]


async def test_ts_gl_association_limit_refuses_a_third_master() -> None:
    h = make_harness()
    server, port = await _start(h)
    masters = [master_mod.Dnp3Master("127.0.0.1", port, timeout_s=1.0) for _ in range(3)]
    try:
        for master in masters[:2]:
            await master.connect()
            await master.integrity_poll()
        await masters[2].connect()
        with pytest.raises(master_mod.Dnp3MasterError):
            await masters[2].integrity_poll()
        assert server.association_count == 2
    finally:
        for master in masters:
            await master.close()
        await server.stop()


async def test_ts_gl_e2e_aen_sim_channel_issues_status_and_cancels_over_dnp3() -> None:
    channel_mod = pytest.importorskip("ogsim.utility_aen.channels.grid_link")
    base_mod = pytest.importorskip("ogsim.utility_aen.channels.base")
    h = make_harness()
    server, port = await _start(h)
    worker = asyncio.create_task(h.service.run())
    channel = channel_mod.build({"host": "127.0.0.1", "port": port, "status_wait_s": 3.0, "timeout_s": 3.0})
    try:
        spec = base_mod.CallSpec(call_ref="77001", kw=-1200.0, start=datetime.now(UTC), duration_min=60)
        result = await channel.issue_call(spec)
        assert result.accepted and result.state == "ACCEPTED", result
        assert h.calls.issued == [("AUSTIN_ENERGY", 77001, 1200.0, 60)]
        assert (await channel.status("77001")).state == "ACCEPTED"
        refused = await channel.issue_call(
            base_mod.CallSpec(call_ref="77002", kw=-9000.0, start=datetime.now(UTC), duration_min=30)
        )
        assert refused.state == "REFUSED" and refused.reason_code == "R-CALL-OVER-COMMITTED"
        charge = await channel.issue_call(
            base_mod.CallSpec(call_ref="77003", kw=500.0, start=datetime.now(UTC), duration_min=30)
        )
        assert charge.reason_code == "R-CALL-CHARGE-REFUSED" and len(h.calls.issued) == 2
        ended = await channel.cancel("77001")
        assert ended.state == "COMPLETED" and h.calls.cancelled == [("AUSTIN_ENERGY", 77001)]
    finally:
        await channel.aclose()
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        await server.stop()
