"""og-safestop's MQTT connections survive a broker disconnect (review 2026-09-26: og-engine's ingest loop
never reconnected). The utility L2 listener resubscribes and still engages; the process fails closed while
a connection is down (no heartbeat, then exit for a systemd restart)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import aiomqtt
import pytest

import opengrid.safestop.l2_intake as l2_intake
from opengrid.core.models.mqtt import ScadaUtilityInstruction
from opengrid.platform.config import Config
from opengrid.safestop.main import stop_path_ready


class _Message:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic, self.payload = topic, payload


class _DroppingClient:
    """Yields its messages, then the broker drops the connection."""

    def __init__(self, messages: list[_Message]) -> None:
        self._messages = messages
        self.subscribed: list[tuple[str, int]] = []

    async def __aenter__(self) -> _DroppingClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def subscribe(self, topic_filter: str, qos: int = 0) -> None:
        self.subscribed.append((topic_filter, qos))

    @property
    def messages(self):
        return self._stream()

    async def _stream(self):
        for m in self._messages:
            yield m
        raise aiomqtt.MqttError("Disconnected during message iteration")


def _block(bank_id: str) -> bytes:
    # Timestamps taken when the message is built, never at import: a long full-suite run would otherwise
    # outlive the 5-minute expiry and the listener would (rightly) ignore the instruction as expired.
    now = datetime.now(UTC)
    return (
        ScadaUtilityInstruction.model_validate(
            {
                "instruction_id": "019842d1-0000-7000-8000-0000000000bb",
                "bank_id": bank_id,
                "kind": "BLOCK",
                "limit_kw": None,
                "issued_at": now,
                "expires_at": now + timedelta(minutes=5),
                "issued_by": "utility",
            }
        )
        .model_dump_json()
        .encode()
    )


async def test_the_l2_listener_reconnects_and_still_engages_after_a_disconnect(monkeypatch):
    cfg = Config({"mqtt": {"topic_root": "ogtest/stop"}})
    batches = [[], [_Message("ogtest/stop/scada/instruction/bank-7", _block("bank-7"))]]
    clients: list[_DroppingClient] = []
    processes: list[str] = []

    def fake_build_client(cfg, *, username, password, process):
        processes.append(process)
        clients.append(_DroppingClient(batches.pop(0)))
        return clients[-1]

    monkeypatch.setattr(l2_intake, "build_client", fake_build_client)
    engaged: list[str] = []

    async def engage(bank_id: str, reason: str, initiator_ref: str) -> object:
        engaged.append(bank_id)
        return None

    async def already_acted(_instruction_id: UUID, _bank_id: str) -> bool:
        return False

    session = l2_intake.build_l2_session(
        cfg, username="og_safestop", password="x", engage_fn=engage, already_acted_fn=already_acted
    )

    async def no_sleep(_s: float) -> None:
        return None

    session._sleep = no_sleep  # type: ignore[method-assign]
    await session.run(max_connections=2)

    assert processes == ["safestop-l2", "safestop-l2"]  # the same client id, one connection at a time
    assert all(c.subscribed == [("ogtest/stop/scada/instruction/+", 1)] for c in clients)
    assert engaged == ["bank-7"]  # the instruction after the reconnect still stops the bank


class _Session:
    def __init__(self, name: str, connected: bool, down_s: float = 0.0) -> None:
        self.name, self.connected, self._down_s = name, connected, down_s

    def down_for_s(self) -> float:
        return self._down_s


def test_safestop_fails_closed_while_a_connection_is_down():
    publish, l2 = _Session("safestop", True), _Session("safestop-l2", True)
    assert stop_path_ready(publish, l2, exit_after_s=30.0)
    assert not stop_path_ready(_Session("safestop", False, 3.0), l2, exit_after_s=30.0)
    assert not stop_path_ready(publish, _Session("safestop-l2", False, 3.0), exit_after_s=30.0)
    with pytest.raises(SystemExit):
        stop_path_ready(_Session("safestop", False, 31.0), l2, exit_after_s=30.0)
