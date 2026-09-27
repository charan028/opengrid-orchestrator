"""Bounded guardian hand-off (`engine.propose_guard`). Prod 2026-09-27 00:17:55 CT: one cycle's propose
phase blocked 25.6 s during a guardian outage, a 31 s grant gap K13 flagged as K13_OUTAGE_GAP."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

from opengrid.engine import GuardianGate, guardian_is_available, propose_all_banks
from opengrid.engine.propose_guard import propose_banks
from opengrid.engine.settings import dispatch_settings
from opengrid.platform.config import Config

NOW = datetime(2026, 9, 27, 5, 17, 55, tzinfo=UTC)


async def test_a_stalled_bank_is_cut_off_at_the_timeout_and_the_others_still_go_out() -> None:
    cancelled: list[str] = []
    done: list[str] = []

    async def _propose(bank_id: str, grants: list[str]) -> None:
        if bank_id == "bank-2":
            try:
                await asyncio.sleep(30)  # the 25.6 s database stall
            except asyncio.CancelledError:
                cancelled.append(bank_id)
                raise
        done.append(bank_id)

    by_bank = {f"bank-{i}": [f"g{i}"] for i in range(6)}
    started = time.monotonic()
    result = await propose_banks(by_bank, _propose, concurrency=4, timeout_s=0.2)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert result.timed_out == ("bank-2",)
    assert result.failed == ("bank-2",)
    assert sorted(done) == sorted(b for b in by_bank if b != "bank-2")
    await asyncio.sleep(0)  # let the cancellation land
    assert cancelled == ["bank-2"]


async def test_the_timeout_does_not_wait_for_a_bank_that_swallows_its_cancellation() -> None:
    release = asyncio.Event()

    async def _propose(bank_id: str, grants: list[str]) -> None:
        while not release.is_set():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await release.wait()  # a driver that is slow to unwind

    started = time.monotonic()
    result = await propose_banks({"bank-0": ["g"]}, _propose, concurrency=4, timeout_s=0.1)
    assert time.monotonic() - started < 1.0
    assert result.timed_out == ("bank-0",)
    release.set()
    await asyncio.sleep(0.01)


async def test_errors_stay_isolated_and_no_timeout_means_unbounded() -> None:
    async def _propose(bank_id: str, grants: list[str]) -> None:
        await asyncio.sleep(0.01)
        if bank_id == "bank-1":
            raise RuntimeError("insert failed")

    result = await propose_banks({"bank-0": [], "bank-1": [], "bank-2": []}, _propose, concurrency=2)
    assert result.failed == ("bank-1",)
    assert result.timed_out == ()
    assert await propose_banks({}, _propose, concurrency=2, timeout_s=0.1) == type(result)()


async def test_a_propose_timeout_marks_the_guardian_stalled() -> None:
    gate = GuardianGate()

    async def _propose(bank_id: str, grants: list[str]) -> None:
        await asyncio.sleep(30)

    failed = await propose_all_banks({"bank-0": ["g"]}, _propose, concurrency=4, timeout_s=0.05, gate=gate)
    assert failed == ["bank-0"]
    assert gate.stalled_at is not None


class _Heartbeats:
    def __init__(self, age_s: float | None, *, delay_s: float = 0.0) -> None:
        self.age_s = age_s
        self.delay_s = delay_s

    async def process_heartbeat_age_s(self, name: str, *, now: datetime | None = None) -> float | None:
        assert name == "guardian"
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        return self.age_s


async def test_the_heartbeat_read_is_bounded_and_a_timeout_means_unavailable() -> None:
    gate = GuardianGate(check_timeout_s=0.05)
    started = time.monotonic()
    ok = await guardian_is_available(_Heartbeats(1.0, delay_s=5.0), now=NOW, miss_threshold_s=15.0, gate=gate)  # type: ignore[arg-type]
    assert ok is False
    assert time.monotonic() - started < 1.0


async def test_missing_or_stale_heartbeat_holds_and_a_fresh_one_proposes() -> None:
    for age, expected in ((None, False), (16.0, False), (4.0, True)):
        assert await guardian_is_available(_Heartbeats(age), now=NOW, miss_threshold_s=15.0) is expected  # type: ignore[arg-type]


async def test_after_a_stall_the_guardian_is_known_unavailable_until_it_beats_again() -> None:
    gate = GuardianGate()
    gate.note_stall(NOW)
    later = NOW + timedelta(seconds=4)
    # Last beat 5 s before `later` = 1 s BEFORE the stall: still held, although within the miss threshold.
    assert await guardian_is_available(_Heartbeats(5.0), now=later, miss_threshold_s=15.0, gate=gate) is False  # type: ignore[arg-type]
    assert gate.stalled_at == NOW
    # A beat 1 s before `later` = after the stall: clears it.
    assert await guardian_is_available(_Heartbeats(1.0), now=later, miss_threshold_s=15.0, gate=gate) is True  # type: ignore[arg-type]
    assert gate.stalled_at is None


def test_the_timeouts_are_configurable_with_2_s_and_half_a_second_defaults() -> None:
    assert dispatch_settings(Config({})).propose_timeout_s == 2.0
    assert dispatch_settings(Config({})).guardian_check_timeout_s == 0.5
    cfg = Config({"allocator": {"propose_timeout_s": 1.5, "guardian_check_timeout_s": 0.25}})
    assert dispatch_settings(cfg).propose_timeout_s == 1.5
    assert dispatch_settings(cfg).guardian_check_timeout_s == 0.25
