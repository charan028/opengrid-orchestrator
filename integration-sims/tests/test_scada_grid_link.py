"""ogsim.scada over the grid-control link: instruction messages -> DNP3 controls, config parsing, ESTOP kept
on MQTT, and `run_scada` routing instructions to the bridge. No opengrid import, no sockets."""

from __future__ import annotations

import contextlib
from typing import Any, cast

from ogsim.protocols.dnp3_master import Dnp3Master
from ogsim.scada.grid_link import GridLinkBridgeSettings, ScadaGridLinkBridge, controls_for

ISSUED = "2026-09-26T20:00:00Z"
TARGETS = {"bank-040": 0, "bank-041": 3}


def _msg(
    kind: str, bank: str = "bank-041", *, limit: float | None = None, lifted: bool = False
) -> dict[str, Any]:
    return {
        "bank_id": bank,
        "kind": kind,
        "limit_kw": limit,
        "issued_at": ISSUED,
        "expires_at": ISSUED if lifted else None,
    }


def test_limit_block_and_lift_map_onto_target_points() -> None:
    assert controls_for(_msg("LIMIT", limit=539.6), TARGETS) == [("AO", 19, 540), ("CROB", 22, 0x03)]
    assert controls_for(_msg("LIMIT", limit=539.6, lifted=True), TARGETS) == [("CROB", 22, 0x04)]
    assert controls_for(_msg("BLOCK", "bank-040"), TARGETS) == [("CROB", 17, 0x03)]
    assert controls_for(_msg("BLOCK", "bank-040", lifted=True), TARGETS) == [("CROB", 17, 0x04)]
    assert controls_for(_msg("ESTOP"), TARGETS) == []
    assert controls_for(_msg("LIMIT", "bank-999", limit=1.0), TARGETS) == []


def test_settings_from_raw_defaults_to_disabled() -> None:
    assert GridLinkBridgeSettings.from_raw(None).enabled is False
    settings = GridLinkBridgeSettings.from_raw(
        {"enabled": True, "port": 20001, "targets": {"bank-040": 0}, "also_mqtt": False, "heartbeat_s": 2}
    )
    assert settings.enabled and settings.port == 20001 and settings.targets == {"bank-040": 0}
    assert settings.also_mqtt is False and settings.heartbeat_s == 2.0


class FakeMaster:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int, bool]] = []
        self.connected = True

    async def analog_output(self, index: int, value: int, *, sbo: bool = False) -> int:
        self.calls.append(("AO", index, value, sbo))
        return 0

    async def crob(self, index: int, code: int, *, sbo: bool = False) -> int:
        self.calls.append(("CROB", index, code, sbo))
        return 0


async def test_bridge_sends_controls_with_sbo_for_crobs_and_skips_estop() -> None:
    master = FakeMaster()
    bridge = ScadaGridLinkBridge(GridLinkBridgeSettings(targets=TARGETS), master=cast(Dnp3Master, master))
    bridge.submit([("scada/instruction/bank-041", _msg("ESTOP")), ("x", _msg("LIMIT", limit=100.0))])
    assert bridge._queue.qsize() == 1
    assert await bridge.send(_msg("LIMIT", limit=100.0)) == [0, 0]
    assert master.calls == [("AO", 19, 100, False), ("CROB", 22, 0x03, True)]
    assert await bridge.heartbeat() == 0 and master.calls[-1] == ("CROB", 2, 0x01, False)


async def test_run_scada_routes_instructions_to_the_bridge(monkeypatch: Any) -> None:
    from ogsim.scada import runtime

    published: list[str] = []

    class Client:
        async def subscribe(self, *_: Any, **__: Any) -> None:
            return None

        async def publish_batch(self, schema: str, items: list[Any], qos: int = 0) -> None:
            published.append(schema)

    class Engine:
        config = type("C", (), {"publish_interval_s": 1.0})()

        def tick(self, now: float) -> tuple[list[Any], list[Any]]:
            return [], [("scada/instruction/bank-041", _msg("BLOCK"))]

    class Clock:
        def now(self) -> float:
            return 0.0

        async def sleep(self, _: float) -> None:
            raise StopAsyncIteration

    settings = GridLinkBridgeSettings(targets=TARGETS, also_mqtt=False)
    bridge = ScadaGridLinkBridge(settings, master=cast(Dnp3Master, FakeMaster()))
    with contextlib.suppress(StopAsyncIteration):
        await runtime.run_scada(cast(Any, Client()), cast(Any, Engine()), cast(Any, Clock()), bridge)
    assert bridge._queue.qsize() == 1
    assert published == ["scada_bank_signal"]  # also_mqtt = false: instructions only over the link
