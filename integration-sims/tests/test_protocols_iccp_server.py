"""ogsim ICCP control-centre simulator: association, bilateral-table access control, reads and transfer
reports over the simulator transport (localhost TCP, OS-assigned port)."""

from __future__ import annotations

import asyncio
import json
import struct
from typing import Any

import pytest

from ogsim.protocols.iccp_server import IccpServerSim, standard_point_names

H = struct.Struct(">I")


async def _send(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
    body = json.dumps(message).encode()
    writer.write(H.pack(len(body)) + body)
    await writer.drain()


async def _recv(reader: asyncio.StreamReader) -> dict[str, Any]:
    (length,) = H.unpack(await asyncio.wait_for(reader.readexactly(4), 2.0))
    message: dict[str, Any] = json.loads(await reader.readexactly(length))
    return message


ASSOC = {
    "op": "associate",
    "bilateral_table_id": "BLT-OPENGRID-01",
    "local_domain": "OPENGRID_ICC",
    "remote_domain": "UTILITY_ICC",
    "tase2_version": "2000-08",
}


@pytest.fixture
async def server() -> Any:
    sim = IccpServerSim(["bank-000"])
    _, port = await sim.start()
    yield sim, port
    await sim.stop()


async def test_associate_read_and_transfer_reports(server: Any) -> None:
    sim, port = server
    sim.set_bank_kva("bank-000", 321.0)
    sim.set_instruction("bank-000", "LIMIT", limit_kw=200.0)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    await _send(writer, ASSOC)
    assert (await _recv(reader))["op"] == "associate_ok"
    await _send(writer, {"op": "read", "names": ["BANK_000_KVA", "BANK_000_LIMACT", "NOPE"]})
    reply = await _recv(reader)
    values = {v["name"]: v for v in reply["values"]}
    assert (
        values["BANK_000_KVA"]["value"] == 321.0 and values["BANK_000_KVA"]["quality"]["validity"] == "VALID"
    )
    assert values["BANK_000_LIMACT"]["value"] == 2.0  # TASE.2 state ON
    assert reply["errors"] == {"NOPE": "object-non-existent"}
    await _send(
        writer, {"op": "start_transfer_set", "dataset": "DS", "names": ["BANK_000_KVA"], "interval_s": 0.02}
    )
    assert (await _recv(reader))["op"] == "transfer_set_started"
    report = await _recv(reader)
    assert report["op"] == "transfer_report" and report["values"][0]["name"] == "BANK_000_KVA"
    await _send(writer, {"op": "conclude"})
    while (await _recv(reader))["op"] != "conclude_ok":
        pass
    writer.close()
    assert sim.associations == 1


async def test_association_and_access_are_enforced(server: Any) -> None:
    sim, port = server
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    await _send(writer, {"op": "read", "names": ["BANK_000_KVA"]})
    assert (await _recv(reader))["code"] == "not-associated"
    writer.close()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    await _send(writer, {**ASSOC, "local_domain": "SOMEONE_ELSE"})
    assert (await _recv(reader))["code"] == "association-rejected"
    writer.close()
    sim.granted.discard("BANK_000_KVA")
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    await _send(writer, ASSOC)
    await _recv(reader)
    await _send(writer, {"op": "start_transfer_set", "names": ["BANK_000_KVA"], "interval_s": 1})
    assert (await _recv(reader))["code"] == "object-access-denied"
    await _send(writer, {"op": "bogus"})
    assert (await _recv(reader))["code"] == "service-not-supported"
    writer.close()


def test_standard_names_and_types() -> None:
    names = standard_point_names(["bank-001"])
    assert names["BANK_001_KVA"] == "Data_RealQ"
    assert names["BANK_001_ESTOP"] == "Data_StateQ"
    assert len(names) == 19
