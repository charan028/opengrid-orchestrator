"""ICCP/TASE.2 adapter against the ogsim ICCP control-centre simulator (simulator transport, localhost TCP)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from opengrid.integrations.scada_iccp.adapter import IccpScadaSource, IccpSettings
from opengrid.integrations.scada_iccp.bilateral import (
    BilateralTable,
    IccpDataValue,
    iccp_quality,
    iccp_state_is_on,
    standard_bilateral_points,
)
from opengrid.integrations.scada_iccp.transport import IccpError, MmsIccpTransport
from opengrid.integrations.sinks import CollectingSink

iccp_sim = pytest.importorskip("ogsim.protocols.iccp_server")
BANKS = ["bank-000", "bank-001"]


def _table(**overrides: Any) -> BilateralTable:
    base: dict[str, Any] = {
        "bilateral_table_id": "BLT-OPENGRID-01",
        "local_domain": "OPENGRID_ICC",
        "remote_domain": "UTILITY_ICC",
        "transfer_interval_s": 0.05,
        "data_values": standard_bilateral_points(BANKS),
    }
    base.update(overrides)
    return BilateralTable(**base)


@pytest.fixture
async def server() -> AsyncIterator[tuple[Any, int]]:
    sim = iccp_sim.IccpServerSim(BANKS)
    _, port = await sim.start()
    assert port >= 18000
    try:
        yield sim, port
    finally:
        await sim.stop()


def _source(port: int, table: BilateralTable | None = None) -> IccpScadaSource:
    settings = IccpSettings(
        host="127.0.0.1",
        port=port,
        timeout_s=1.0,
        reconnect_min_s=0.05,
        reconnect_max_s=0.1,
        bilateral_table=table or _table(),
    )
    return IccpScadaSource(settings)


async def test_read_maps_reals_and_states(server: tuple[Any, int]) -> None:
    sim, port = server
    sim.set_bank_kva("bank-000", 433.5)
    sim.set_value("BANK_001_KVA", 120.0, validity="HELD")
    sim.set_instruction("bank-001", "LIMIT", limit_kw=300.0)
    source = _source(port)
    sink = CollectingSink()
    try:
        await source.poll_once(sink)
    finally:
        await source.close()
    reading = sink.latest("bank-000", "APPARENT_POWER_KVA")
    assert reading is not None and reading.value == pytest.approx(433.5) and reading.quality == "good"
    assert sink.latest("bank-001", "APPARENT_POWER_KVA") is None  # HELD -> stale -> withheld
    assert [(i.bank_id, i.kind, i.limit_kw) for i in sink.instructions] == [("bank-001", "LIMIT", 300.0)]


async def test_association_rejected_on_bilateral_mismatch(server: tuple[Any, int]) -> None:
    _, port = server
    source = _source(port, _table(bilateral_table_id="BLT-WRONG"))
    with pytest.raises(IccpError, match="association-rejected"):
        await source.poll_once(CollectingSink())
    await source.close()


async def test_read_of_ungranted_value_is_refused(server: tuple[Any, int]) -> None:
    sim, port = server
    sim.granted.discard("BANK_000_KVA")
    source = _source(port)
    with pytest.raises(IccpError, match="object-access-denied"):
        await source.poll_once(CollectingSink())
    await source.close()


async def test_transfer_set_reports_drive_run_and_reconnect(server: tuple[Any, int]) -> None:
    sim, port = server
    sim.set_bank_kva("bank-000", 100.0)
    source = _source(port)
    sink = CollectingSink()
    task = asyncio.create_task(source.run(sink))
    try:
        await _until(lambda: _kva(sink) == pytest.approx(100.0))
        sim.set_bank_kva("bank-000", 250.0)
        sim.set_instruction("bank-000", "ESTOP")
        await _until(lambda: _kva(sink) == pytest.approx(250.0) and bool(sink.instructions))
        assert sink.instructions[-1].kind == "ESTOP"
        # drop every connection: the adapter re-associates and resumes
        await sim.stop()
        sim2 = iccp_sim.IccpServerSim(BANKS, port=port)
        await sim2.start()
        try:
            sim2.set_bank_kva("bank-000", 321.0)
            await _until(lambda: _kva(sink) == pytest.approx(321.0))
        finally:
            await sim2.stop()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await source.close()


def _kva(sink: CollectingSink) -> float | None:
    reading = sink.latest("bank-000", "APPARENT_POWER_KVA")
    return reading.value if reading is not None else None


async def _until(predicate: Any, timeout_s: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


async def test_mms_transport_fails_loudly() -> None:
    with pytest.raises(NotImplementedError, match=r"vendor TASE\.2 stack"):
        await MmsIccpTransport().associate(_table())


def test_bilateral_table_validation() -> None:
    with pytest.raises(ValueError, match="analog role"):
        IccpDataValue(name="X", type="Data_RealQ", bank_id="b", role="UTILITY_ESTOP")
    with pytest.raises(ValueError, match="status role"):
        IccpDataValue(name="X", type="Data_StateQ", bank_id="b", role="APPARENT_POWER_KVA")
    dup = standard_bilateral_points(["b"])
    with pytest.raises(ValueError, match="duplicate"):
        _table(data_values=[*dup, dup[0]])


def test_quality_and_state_mapping() -> None:
    assert iccp_quality("VALID") == "good"
    assert iccp_quality("held") == "stale"
    assert iccp_quality("NOTVALID") == "comm_fail"
    assert iccp_state_is_on(2) == (True, True)
    assert iccp_state_is_on(1) == (False, True)
    assert iccp_state_is_on(0) == (False, False)
    assert iccp_state_is_on(3) == (False, False)
