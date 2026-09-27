"""Manual operator setpoints ramped by the engine within G-04 (R-MANUAL-RAMP)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from opengrid import engine
from opengrid.core.models.engine import Grant
from opengrid.engine.manual import (
    R_MANUAL_RAMP,
    ManualTarget,
    ManualTargetSource,
    apply_stops,
    manual_items,
    parse_targets,
)

NOW = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)


def _row(hubs, kw, *, issued=NOW, expires=NOW + timedelta(minutes=10)):
    return (
        uuid4(),
        {
            "hub_ids": hubs,
            "p_kw_target": kw,
            "issued_at": issued.isoformat(),
            "expires_at": expires.isoformat(),
        },
        issued,
    )


def test_newest_target_wins_and_expired_or_malformed_are_ignored() -> None:
    rows = [
        _row(["h1", "h2"], -5.0, issued=NOW - timedelta(minutes=2)),
        _row(["h1"], -8.0, issued=NOW - timedelta(minutes=1)),
        _row(["h3"], -2.0, expires=NOW - timedelta(seconds=1)),
        (uuid4(), {"hub_ids": ["h4"]}, NOW),
    ]
    targets = parse_targets(rows, NOW)
    assert {h: t.p_kw_target for h, t in targets.items()} == {"h1": -8.0, "h2": -5.0}


def test_a_safe_stop_cancels_covering_targets() -> None:
    issued = NOW - timedelta(minutes=5)
    targets = {
        "h1": ManualTarget("h1", -5.0, issued, NOW + timedelta(minutes=5), "t1"),
        "h2": ManualTarget("h2", -5.0, issued, NOW + timedelta(minutes=5), "t1"),
    }
    bank_of = {"h1": "b1", "h2": "b2"}.get
    zone_of = {"b1": "LZ_NORTH", "b2": "LZ_WEST"}.get
    stop_b1 = [("BANK", "b1", "ENGAGE", NOW - timedelta(minutes=1)), ("BANK", "b1", "RELEASE", NOW)]
    assert set(apply_stops(targets, stop_b1, bank_of_hub=bank_of, zone_of_bank=zone_of)) == {"h2"}
    old_released = [
        ("ZONE", "LZ_WEST", "ENGAGE", NOW - timedelta(hours=1)),
        ("ZONE", "LZ_WEST", "RELEASE", issued),
    ]
    assert set(apply_stops(targets, old_released, bank_of_hub=bank_of, zone_of_bank=zone_of)) == {"h1", "h2"}
    fleet_engaged = [("FLEET", "FLEET", "ENGAGE", NOW - timedelta(hours=1))]
    assert apply_stops(targets, fleet_engaged, bank_of_hub=bank_of, zone_of_bank=zone_of) == {}


@dataclass
class _Hub:
    hub_id: str
    bank_id: str
    free_discharge_kw: float
    health: str = "online"
    p_kw: float | None = 0.0
    ramp_kw_per_s: float = 0.0611  # 11 kW over 180 s


def test_manual_items_step_toward_the_target_within_the_ramp() -> None:
    targets = {"h1": ManualTarget("h1", -8.0, NOW, NOW + timedelta(minutes=5), "t1")}
    hubs = [_Hub("h1", "b1", 10.0), _Hub("h2", "b1", 10.0)]
    items = manual_items("b1", targets, hubs, lambda hub, kw: engine._ramped_setpoint_kw(hub, kw, 2.0))
    (item,) = items
    assert item["hub_id"] == "h1" and item["reason_code"] == R_MANUAL_RAMP and item["obligation_id"] is None
    assert item["p_kw_setpoint"] == pytest.approx(-0.0611 * 2.0 * engine.RAMP_SAFETY_FACTOR)
    at_target = [_Hub("h1", "b1", 10.0, p_kw=-8.0)]
    (held,) = manual_items("b1", targets, at_target, lambda hub, kw: engine._ramped_setpoint_kw(hub, kw, 2.0))
    assert held["p_kw_setpoint"] == pytest.approx(-8.0)  # reached: held until expiry


@dataclass
class _Backend:
    inserted: list[Any] = field(default_factory=list)
    notified: list[Any] = field(default_factory=list)

    async def insert_command_batch(self, row):
        self.inserted.append(row)

    async def notify_guardian(self, batch_id):
        self.notified.append(batch_id)


@dataclass
class _Trace:
    appended: list[tuple[str, str, str, dict]] = field(default_factory=list)

    async def append(self, stream_id, decision_type, event_class, payload, reason_codes=None):
        from opengrid.trace.store import TraceRecordRef

        self.appended.append((stream_id, decision_type, event_class, payload))
        return TraceRecordRef(trace_id=uuid4(), stream_id=stream_id, seq=len(self.appended), hash="h")


class _Fleet:
    def __init__(self, hubs):
        self._hubs = hubs

    def hub_capabilities(self, bank_id):
        return [h for h in self._hubs if h.bank_id == bank_id]

    def known_bank_ids(self):
        return sorted({h.bank_id for h in self._hubs})


@pytest.mark.asyncio
async def test_a_bank_with_only_a_manual_target_gets_a_batch_and_manual_hubs_get_no_dispatch_share() -> None:
    fleet = _Fleet([_Hub("h1", "b1", 10.0), _Hub("h2", "b1", 10.0)])
    targets = {"h1": ManualTarget("h1", -8.0, NOW, NOW + timedelta(minutes=5), "t1")}
    trace, backend = _Trace(), _Backend()
    batch = await engine.propose_batch_to_guardian(
        backend=backend,
        trace=trace,
        fleet_module=fleet,
        cycle_id="c",
        bank_id="b1",
        grants=[],
        ledger_version=1,
        epoch=1,
        seq=1,
        now=NOW,
        cycle_interval_s=2.0,
        manual_targets=targets,
    )
    assert batch is not None
    (item,) = trace.appended[0][3]["items"]
    assert item["hub_id"] == "h1" and item["reason_code"] == R_MANUAL_RAMP
    headroom = Grant(
        grant_id=uuid4(),
        cycle_id="c",
        obligation_id=None,
        bank_id="b1",
        granted_kw=Decimal("6"),
        is_headroom=True,
        ledger_version=1,
    )
    await engine.propose_batch_to_guardian(
        backend=backend,
        trace=trace,
        fleet_module=fleet,
        cycle_id="c2",
        bank_id="b1",
        grants=[headroom],
        ledger_version=1,
        epoch=1,
        seq=2,
        now=NOW,
        cycle_interval_s=2.0,
        manual_targets=targets,
    )
    items = trace.appended[1][3]["items"]
    assert {i["hub_id"] for i in items if i["reason_code"] == "R-GRANT-HEADROOM"} == {"h2"}
    assert engine.manual_bank_ids(targets, fleet) == ["b1"]


class _Cursor:
    def __init__(self, responses):
        self._responses = list(responses)
        self._current = None

    async def execute(self, sql, params=None):
        self._current = self._responses.pop(0) if self._responses else []

    async def fetchall(self):
        return self._current or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self, cursor):
        self._cursor = cursor

    def connection(self):
        pool = self

        class _C:
            def cursor(self):
                return pool._cursor

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        return _C()


@pytest.mark.asyncio
async def test_source_reads_targets_and_stops_and_keeps_the_last_set_on_error() -> None:
    now = datetime.now(UTC)
    rows = [_row(["h1"], -4.0, issued=now, expires=now + timedelta(minutes=5))]
    source = ManualTargetSource(
        _Pool(_Cursor([rows, []])),
        bank_of_hub=lambda h: "b1",
        zone_of_bank=lambda b: "LZ_NORTH",
        refresh_s=0.0,
    )
    assert set(await source.targets(now)) == {"h1"}
    assert source.active_hub_ids() == frozenset({"h1"})

    class _Broken:
        def connection(self):
            raise RuntimeError("db down")

    source._pool = _Broken()
    assert set(await source.targets(now)) == {"h1"}


def test_operator_override_grants_carry_their_reason_onto_hub_items() -> None:
    from opengrid.core.reasons import R_OPERATOR_OVERRIDE

    fleet = _Fleet([_Hub("h2", "b1", 10.0)])
    grant = Grant(
        grant_id=uuid4(),
        cycle_id="c",
        obligation_id=uuid4(),
        bank_id="b1",
        granted_kw=Decimal("5"),
        is_headroom=False,
        ledger_version=1,
        reason_code=R_OPERATOR_OVERRIDE,
    )
    (item,) = engine._distribute_hub_items("b1", [grant], fleet_module=fleet)
    assert item["reason_code"] == R_OPERATOR_OVERRIDE


# --- sign convention: one convention, +charge / -discharge, end to end -------------------------------------


def _ramp_items(target_kw: float, measured_kw: float, convention: str | None = "+charge/-discharge"):
    payload = {
        "hub_ids": ["h1"],
        "p_kw_command": target_kw,
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
    }
    if convention is not None:
        payload["sign_convention"] = convention
    targets = parse_targets([(uuid4(), payload, NOW)], NOW)
    hubs = [_Hub("h1", "b1", 10.0, p_kw=measured_kw)]
    return manual_items("b1", targets, hubs, lambda hub, kw: engine._ramped_setpoint_kw(hub, kw, 2.0))


def test_a_charge_target_makes_the_hub_charge_and_a_discharge_target_makes_it_discharge() -> None:
    """Command, telemetry and physics share +charge/-discharge: a +target steps up and raises SoC."""
    from opengrid.core.physics import HubParams, soc_step

    params = HubParams(e_kwh=39.2, r_kwh=7.84, p_kw=11.0)
    (charge,) = _ramp_items(+6.0, 0.0)
    assert charge["p_kw_setpoint"] > 0
    assert soc_step(20.0, float(charge["p_kw_setpoint"]), 60.0, params) > 20.0  # SoC rises: charging
    (discharge,) = _ramp_items(-6.0, 0.0)
    assert discharge["p_kw_setpoint"] < 0
    assert soc_step(20.0, float(discharge["p_kw_setpoint"]), 60.0, params) < 20.0  # SoC falls: discharging
    # From a hub measured discharging at 5 kW (telemetry -5), a charge target moves it TOWARD charge.
    (reverse,) = _ramp_items(+6.0, -5.0)
    assert reverse["p_kw_setpoint"] > -5.0


def test_an_unknown_sign_convention_is_refused_and_the_old_field_name_still_reads() -> None:
    assert _ramp_items(+6.0, 0.0, convention="+discharge/-charge") == []
    legacy = parse_targets(
        [
            (
                uuid4(),
                {
                    "hub_ids": ["h1"],
                    "p_kw_target": -3.0,
                    "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
                },
                NOW,
            )
        ],
        NOW,
    )
    assert legacy["h1"].p_kw_target == -3.0


def test_a_cancel_ends_only_the_named_target_even_without_a_command_value() -> None:
    """FOLLOWUPS' cancel row: same hubs, expires_at == issued_at, "cancels": <original trace id>."""
    original = uuid4()
    later = NOW + timedelta(seconds=30)
    first = {
        "hub_ids": ["h1", "h2"],
        "p_kw_command": -5.0,
        "sign_convention": "+charge/-discharge",
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
    }
    cancel = {
        "hub_ids": ["h1", "h2"],
        "cancels": str(original),
        "issued_at": later.isoformat(),
        "expires_at": later.isoformat(),
    }
    rows = [
        (original, first, NOW),
        _row(["h2"], -2.0, issued=NOW + timedelta(seconds=10)),  # a newer target on h2 survives the cancel
        (uuid4(), cancel, later),
    ]
    targets = parse_targets(rows, later)
    assert set(targets) == {"h2"} and targets["h2"].p_kw_target == -2.0
