"""`engine.ramp_anchor` (HIGH-A): a utility-scale hub steps from its last SIGNED setpoint, never its last
proposal; only a lapsed lease or a safe stop re-anchors it (a veto does not)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from opengrid import engine
from opengrid.core.manual_targets import latest_stop_by_scope, stop_covers
from opengrid.engine.ramp_anchor import RampAnchors, SignedSetpoint, stopped_banks

NOW = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)
LEASE = timedelta(seconds=30)


@dataclass
class _Hub:
    hub_id: str = "sub-1"
    bank_id: str = "bank-sub"
    p_kw: float = 0.0
    last_seen_at: datetime | None = NOW - timedelta(seconds=5)
    ramp_kw_per_s: float = 50.0
    utility_scale: bool = True


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> RampAnchors:
    anchors = RampAnchors()
    monkeypatch.setattr(engine, "_ramp_anchors", anchors)
    return anchors


def _propose(anchors: RampAnchors, hub: _Hub, target: float, now: datetime) -> tuple[float, object]:
    sp = engine._ramped_setpoint_kw(hub, target, 2.0, now=now)
    batch = uuid4()
    anchors.record_proposal(batch, [{"hub_id": hub.hub_id, "p_kw_setpoint": sp}], now + LEASE)
    return sp, batch


def test_unsigned_proposals_never_move_the_anchor(_fresh: RampAnchors) -> None:
    """The HIGH-A bug: every proposal advanced the anchor, signed or not."""
    hub = _Hub()
    step = 50.0 * 2.0 * engine.RAMP_SAFETY_FACTOR
    for k in range(10):  # ten cycles, no verdict ever arrives (guardian not signing)
        sp, _ = _propose(_fresh, hub, -20_000.0, NOW + timedelta(seconds=2 * k))
        assert sp == pytest.approx(-step)  # always one step from telemetry 0, never walking away


def test_a_signed_setpoint_is_the_anchor_while_its_lease_is_live(_fresh: RampAnchors) -> None:
    hub = _Hub()
    step = 50.0 * 2.0 * engine.RAMP_SAFETY_FACTOR
    sp1, b1 = _propose(_fresh, hub, -20_000.0, NOW)
    _fresh.apply_verdicts({b1: "PASS"}, NOW + timedelta(seconds=2))  # type: ignore[dict-item]
    sp2, _ = _propose(_fresh, hub, -20_000.0, NOW + timedelta(seconds=2))
    assert sp2 == pytest.approx(sp1 - step)  # telemetry still 0 (lagging): the signature decides
    # Lease lapsed (no newer signature): back to telemetry.
    sp3 = engine._ramped_setpoint_kw(hub, -20_000.0, 2.0, now=NOW + timedelta(seconds=40))
    assert sp3 == pytest.approx(-step)


@pytest.mark.parametrize("outcome", ["VETOED", "PARTLY_VETOED"])
def test_a_veto_keeps_the_live_lease_signed_anchor_and_the_retry_steps_from_it(
    _fresh: RampAnchors, outcome: str
) -> None:
    """r3.4.3 HIGH: dropping the signed anchor on a veto stepped from stale telemetry while the hub still held
    its signed setpoint -- the guardian then signed 4-5x the bound toward the stale reading."""
    hub = _Hub(p_kw=0.0)  # stale telemetry; the hub really holds the signed -1,000 kW
    _fresh.signed["sub-1"] = SignedSetpoint(-1000.0, NOW, NOW + LEASE)
    _fresh.utility_hubs["sub-1"] = "bank-sub"
    _, b2 = _propose(_fresh, hub, -20_000.0, NOW + timedelta(seconds=2))
    _fresh.apply_verdicts({b2: outcome}, NOW + timedelta(seconds=4))  # type: ignore[dict-item]
    assert _fresh.anchor_kw(hub, NOW + timedelta(seconds=4)) == -1000.0
    step = 50.0 * 2.0 * engine.RAMP_SAFETY_FACTOR
    retry = engine._ramped_setpoint_kw(hub, -20_000.0, 2.0, now=NOW + timedelta(seconds=4))
    assert retry == pytest.approx(-1000.0 - step)  # one step from the signature, never from 0
    # Only a lapsed lease (or a stop) lets telemetry back in.
    assert _fresh.anchor_kw(hub, NOW + timedelta(seconds=31)) == 0.0


def test_a_hub_with_several_items_is_recorded_at_their_sum(_fresh: RampAnchors) -> None:
    _fresh.utility_hubs["sub-1"] = "bank-sub"
    batch = uuid4()
    _fresh.record_proposal(
        batch,
        [{"hub_id": "sub-1", "p_kw_setpoint": 0.0}, {"hub_id": "sub-1", "p_kw_setpoint": -490.0}],
        NOW + LEASE,
    )
    _fresh.apply_verdicts({batch: "PASS"}, NOW)
    assert _fresh.signed["sub-1"].kw == -490.0


def test_a_batch_with_no_verdict_is_forgotten_when_its_lease_ends(_fresh: RampAnchors) -> None:
    hub = _Hub()
    _propose(_fresh, hub, -1000.0, NOW)
    _fresh.apply_verdicts({}, NOW + timedelta(seconds=31))
    assert _fresh.pending == {}


def test_a_safe_stop_drops_the_anchor_then_holds_zero_until_fresh_telemetry(_fresh: RampAnchors) -> None:
    hub = _Hub(p_kw=-9_000.0)  # stale pre-stop telemetry
    _, b1 = _propose(_fresh, hub, -20_000.0, NOW)
    _fresh.apply_verdicts({b1: "PASS"}, NOW)  # type: ignore[dict-item]
    engaged = {("ZONE", "LZ_AUSTIN"): ("ENGAGE", NOW + timedelta(seconds=1))}
    _fresh.apply_stops(engaged, zone_of_bank=lambda b: "LZ_AUSTIN")
    assert _fresh.stopped == {"sub-1"} and "sub-1" not in _fresh.signed
    released_at = NOW + timedelta(seconds=41)
    _fresh.apply_stops({("ZONE", "LZ_AUSTIN"): ("RELEASE", released_at)}, zone_of_bank=lambda b: "LZ_AUSTIN")
    assert _fresh.stopped == set()
    hub.last_seen_at = released_at - timedelta(seconds=3)  # telemetry from before the release: not trusted
    assert _fresh.anchor_kw(hub, released_at + timedelta(seconds=2)) == 0.0
    hub.last_seen_at, hub.p_kw = released_at + timedelta(seconds=4), -5.0
    assert _fresh.anchor_kw(hub, released_at + timedelta(seconds=6)) == -5.0


def test_a_pass_for_a_hub_under_a_stop_is_not_an_anchor(_fresh: RampAnchors) -> None:
    hub = _Hub()
    _, b1 = _propose(_fresh, hub, -1000.0, NOW)
    _fresh.apply_stops({("FLEET", "*"): ("ENGAGE", NOW)}, zone_of_bank=lambda b: None)
    _fresh.apply_verdicts({b1: "PASS"}, NOW + timedelta(seconds=1))  # type: ignore[dict-item]
    assert "sub-1" not in _fresh.signed


def test_stopped_banks_and_the_shared_scope_rule() -> None:
    rows = [
        ("s1", "BANK", "b1", "ENGAGE", NOW),
        ("s2", "ZONE", "LZ_WEST", "ENGAGE", NOW),
        ("s3", "ZONE", "LZ_WEST", "RELEASE", NOW + timedelta(seconds=5)),
    ]
    latest = latest_stop_by_scope(rows)
    assert latest[("ZONE", "LZ_WEST")][0] == "RELEASE"
    zones = {"b1": "LZ_WEST", "b2": "LZ_WEST", "b3": "LZ_AUSTIN"}
    assert stopped_banks(["b1", "b2", "b3"], latest, zone_of_bank=zones.get) == {"b1"}
    assert stop_covers("FLEET", "*", bank="b3", zone=None)


def test_the_engine_proposes_nothing_for_a_stopped_bank() -> None:
    source = SimpleNamespace(stop_scopes=lambda: {("BANK", "b1"): ("ENGAGE", NOW)})
    state = SimpleNamespace(manual_source=source)
    fleet = SimpleNamespace(bank_zone=lambda b: "LZ")
    by_bank: dict = {"b1": ["g1"], "b2": ["g2"]}
    engine.drop_stopped_banks(state, by_bank, fleet)
    assert list(by_bank) == ["b2"]


def test_refresh_reads_only_pending_batches_and_survives_a_slow_read(_fresh: RampAnchors) -> None:
    calls: list[list] = []

    async def _outcomes(ids: list) -> dict:
        calls.append(ids)
        return {}

    asyncio.run(_fresh.refresh(_outcomes, NOW))
    assert calls == []  # nothing pending: no read at all
    _propose(_fresh, _Hub(), -1000.0, NOW)

    async def _slow(ids: list) -> dict:
        await asyncio.sleep(5)
        return {}

    asyncio.run(_fresh.refresh(_slow, NOW, timeout_s=0.05))
    assert len(_fresh.pending) == 1  # kept for the next cycle
