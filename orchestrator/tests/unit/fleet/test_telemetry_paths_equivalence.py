"""r3.4.5 gate for `[ingest].telemetry_decoupled`: the inline path (default) and the decoupled path
(`engine.telemetry_ingest`) leave the fleet twin in IDENTICAL state for the same in-order message stream."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from opengrid import fleet
from opengrid.core.models.platform import Bank, Hub, HubState
from opengrid.engine.telemetry_ingest import TelemetryDecoupler
from opengrid.platform.config import Config
from opengrid.platform.mqtt import validate_payload

HUBS = [f"h{i:02d}" for i in range(20)]
T0 = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=60)


@dataclass
class _Backend:
    hubs: list[Hub] = field(default_factory=list)
    banks: list[Bank] = field(default_factory=list)
    copied_rows: list[Any] = field(default_factory=list)

    async def load_hubs(self) -> list[Hub]:
        return self.hubs

    async def load_banks(self) -> list[Bank]:
        return self.banks

    async def load_hub_states(self) -> list[HubState]:
        return []

    async def upsert_hub_states(self, states: list[HubState]) -> None:
        return None

    async def copy_telemetry(self, rows: list[Any]) -> None:
        self.copied_rows.extend(rows)

    async def record_scada_observations(self, signals: Any) -> None:
        return None

    async def insert_acks(self, acks: Any) -> None:
        return None


def _cfg() -> Config:
    return Config(
        {"health": {"hub_stale_s": 600.0, "hub_offline_s": 3600.0}, "fleet": {"telemetry_interval_s": 2.0}}
    )


async def _seed() -> _Backend:
    backend = _Backend(
        hubs=[
            Hub(hub_id=h, bank_id=f"bank-{i % 4}", zone="LZ_NORTH", e_kwh=40.0, r_kwh=8.0, p_kw=11.0)
            for i, h in enumerate(HUBS)
        ],
        banks=[
            Bank(bank_id=f"bank-{b}", zone="LZ_NORTH", kva_rating=600.0, reserve_kva=0.0) for b in range(4)
        ],
    )
    fleet.configure(backend, _cfg())  # type: ignore[arg-type]
    await fleet.load_topology()
    return backend


def _stream(ticks: int, seed: int = 7) -> list[list[bytes]]:
    """`ticks` rounds, each one message per hub (shuffled within the round), in time order per hub."""
    rng = random.Random(seed)  # noqa: S311 -- a reproducible test stream, not crypto
    rounds: list[list[bytes]] = []
    for k in range(ticks):
        msgs = []
        for i, hub in enumerate(HUBS):
            payload: dict[str, Any] = {
                "hub_id": hub,
                "bank_id": f"bank-{i % 4}",
                "zone": "LZ_NORTH",
                "ts": (T0 + timedelta(seconds=2 * k, milliseconds=i)).isoformat(),
                "soc_kwh": round(rng.uniform(9.0, 38.0), 3),
                "p_kw": round(rng.uniform(-11.0, 11.0), 3),
                "health": "online",
                "seq": k + 1,
                "epoch": 1,
            }
            if rng.random() < 0.5:
                payload |= {
                    "cell_temp_c": round(rng.uniform(20, 45), 1),
                    "meter_kw": round(rng.uniform(-3, 3), 2),
                }
            msgs.append(json.dumps(payload).encode())
        rng.shuffle(msgs)
        rounds.append(msgs)
    return rounds


def _twin_state() -> dict[str, tuple[Any, ...]]:
    return {
        hub_id: (rt.soc_kwh, rt.p_kw, rt.fault_code, rt.last_seen_at, rt.seq, rt.epoch, dict(rt.flow or {}))
        for hub_id, rt in sorted(fleet._hubs.items())
    }


def _capabilities() -> list[Any]:
    return [fleet.hub_capabilities(f"bank-{b}") for b in range(4)]


async def _run_inline(rounds: list[list[bytes]]) -> tuple[dict, list, list]:
    backend = await _seed()
    for msgs in rounds:
        for raw in msgs:  # the pre-r3.4.5 inline path, as `_mqtt_ingest_loop` runs it
            payload = json.loads(raw)
            validate_payload("telemetry", payload)
            await fleet.ingest_telemetry(payload)
    state, caps = _twin_state(), _capabilities()
    await fleet.flush()
    return state, caps, [(r.hub_id, r.ts, r.soc_kwh, r.p_kw) for r in backend.copied_rows]


async def _run_decoupled(rounds: list[list[bytes]]) -> tuple[dict, list, list]:
    backend = await _seed()

    async def _apply(tel: Any) -> None:
        fleet.apply_telemetry(tel)

    decoupler = TelemetryDecoupler(fleet.parse_telemetry, _apply)
    for msgs in rounds:
        for raw in msgs:
            decoupler.parse_one(raw)  # what the parser thread does, deterministically
        await decoupler.apply_once()  # one apply per 2 s round (the applier runs every 100 ms)
    state, caps = _twin_state(), _capabilities()
    await fleet.flush()
    return state, caps, [(r.hub_id, r.ts, r.soc_kwh, r.p_kw) for r in backend.copied_rows]


@pytest.fixture(autouse=True)
def _reset() -> Any:
    yield
    fleet.configure(_Backend(), _cfg())  # type: ignore[arg-type]


async def test_both_paths_leave_an_identical_twin_and_identical_telemetry_rows() -> None:
    rounds = _stream(ticks=15)
    inline = await _run_inline(rounds)
    decoupled = await _run_decoupled(rounds)
    assert decoupled[0] == inline[0]  # twin runtime state, hub by hub
    assert decoupled[1] == inline[1]  # hub capability snapshots (health, soc, p_kw, flow, ...)
    assert sorted(decoupled[2], key=str) == sorted(inline[2], key=str)  # persisted rows


async def test_a_burst_within_one_apply_interval_still_ends_in_the_same_twin_state() -> None:
    """Several samples of a hub inside one 100 ms apply: the twin ends identical (newest wins); only the
    superseded intermediate rows are not persisted (documented in docs/orchestrator/ingest-decoupling.md)."""
    rounds = _stream(ticks=6, seed=11)
    burst = [[raw for msgs in rounds for raw in msgs]]  # everything in ONE apply interval
    inline = await _run_inline(rounds)
    decoupled = await _run_decoupled(burst)
    assert decoupled[0] == inline[0]
    assert decoupled[1] == inline[1]
    assert len(decoupled[2]) == len(HUBS)
