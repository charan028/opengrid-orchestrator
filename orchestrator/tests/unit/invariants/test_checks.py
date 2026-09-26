"""Unit tests for `opengrid.invariants.checks`' pure violation-detection logic (00-invariants.md K1,
K2, K13, orphan reservations/commitments): each check must detect a seeded violation and report none on
clean data (BUILD.md task brief's own acceptance bar for these tests)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from opengrid.allocator.models import HubSnapshot
from opengrid.invariants import checks

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)


# --- K1: reserve breach ------------------------------------------------------------------------------


def test_find_reserve_breaches_detects_discharge_below_reserve() -> None:
    rows = [("hub-1", NOW, 1.5, -5.0, 2.0)]  # soc 1.5 < reserve 2.0, discharging
    violations = checks.find_reserve_breaches(rows)
    assert len(violations) == 1
    assert violations[0].scope == {"hub_id": "hub-1", "ts": NOW.isoformat()}
    assert violations[0].detail["soc_kwh"] == 1.5
    assert violations[0].dedupe_key == f"hub-1|{NOW.isoformat()}"


def test_find_reserve_breaches_clean_when_above_reserve() -> None:
    rows = [("hub-1", NOW, 5.0, -5.0, 2.0)]  # soc above reserve
    assert checks.find_reserve_breaches(rows) == []


def test_find_reserve_breaches_clean_when_idle_or_charging_below_reserve() -> None:
    """Sitting below reserve while NOT discharging (idle, or charging back up) is not itself a K1
    breach -- see the function's own docstring."""
    rows = [
        ("hub-1", NOW, 1.0, 0.0, 2.0),  # idle
        ("hub-2", NOW, 1.0, 3.0, 2.0),  # charging
    ]
    assert checks.find_reserve_breaches(rows) == []


# --- K2: double-sold kWh (capability, not nameplate) --------------------------------------------------


def test_find_double_sold_detects_over_true_capacity_bank_interval() -> None:
    start = NOW
    end = NOW + timedelta(minutes=15)
    rows = [("bank-000", start, end, 650.0)]  # 50 kW over the bank's TRUE (admitted) capability
    violations = checks.find_double_sold(rows, capability_by_bank={"bank-000": 600.0})
    assert len(violations) == 1
    assert violations[0].detail["excess_kw"] == 50.0
    assert violations[0].dedupe_key == f"bank-000|{start.isoformat()}"
    # 50 kW excess over a 15-minute (0.25h) interval = 12.5 kWh sold twice.
    assert violations[0].magnitude == 12.5


def test_find_double_sold_clean_when_within_true_capacity() -> None:
    rows = [("bank-000", NOW, NOW + timedelta(minutes=15), 550.0)]
    assert checks.find_double_sold(rows, capability_by_bank={"bank-000": 600.0}) == []


def test_find_double_sold_detects_reservations_over_rated_capability() -> None:
    rows = [("bank-000", NOW, NOW + timedelta(minutes=15), 400.0)]
    violations = checks.find_double_sold(rows, capability_by_bank={"bank-000": 300.0})
    assert len(violations) == 1
    assert violations[0].detail["excess_kw"] == 100.0


def test_find_double_sold_treats_bank_with_no_capability_row_as_zero_capacity() -> None:
    rows = [("bank-999", NOW, NOW + timedelta(minutes=15), 1.0)]
    violations = checks.find_double_sold(rows, capability_by_bank={})
    assert len(violations) == 1
    assert violations[0].detail["capability_kw"] == 0.0


_ROW = tuple[str, float, float, float, float, float, float, float, float, str, int | None]


def test_rated_capability_ignores_health_and_soc_and_caps_at_bank_rating() -> None:
    """K2 is judged against RATED capability: an offline hub or an empty battery still counts, and the
    bank's kVA (minus reserve) caps the sum."""
    hub_rows: list[_ROW] = [
        # (bank_id, kva_rating, reserve_kva, e_kwh, r_kwh, p_kw, eta_c, eta_d, soc_kwh, health, units)
        ("bank-000", 600.0, 0.0, 39.2, 7.84, 11.0, 0.9487, 0.9487, 39.2, "online", 1),
        ("bank-000", 600.0, 0.0, 39.2, 7.84, 11.0, 0.9487, 0.9487, 0.0, "offline", 1),
        ("bank-000", 600.0, 0.0, 39.2, 7.84, 11.0, 0.9487, 0.9487, 7.84, "fault", 1),
        ("bank-001", 25.0, 5.0, 78.4, 15.68, 20.0, 0.9487, 0.9487, 78.4, "online", 2),
        ("bank-001", 25.0, 5.0, 78.4, 15.68, 20.0, 0.9487, 0.9487, 78.4, "online", 2),
    ]
    result = checks.compute_bank_rated_capabilities_kw(hub_rows)
    assert result["bank-000"] == pytest.approx(33.0)
    assert result["bank-001"] == pytest.approx(20.0)  # 40 kW of hubs, capped at 25 - 5 kVA


def test_rated_capability_applies_the_unit_cap() -> None:
    """K2 units (migration 0032): a dual-unit home counts at 20 kW; a single-unit home mis-seeded at 20 kW
    and a home with an unknown unit count (pre-0032 database) both fail closed to 11 kW."""
    hub_rows: list[_ROW] = [
        ("bank-000", 600.0, 0.0, 78.4, 15.68, 20.0, 0.9487, 0.9487, 78.4, "online", 2),
        ("bank-000", 600.0, 0.0, 39.2, 7.84, 20.0, 0.9487, 0.9487, 39.2, "online", 1),
        ("bank-000", 600.0, 0.0, 78.4, 15.68, 20.0, 0.9487, 0.9487, 78.4, "online", None),
    ]
    result = checks.compute_bank_rated_capabilities_kw(hub_rows)
    assert result["bank-000"] == pytest.approx(20.0 + 11.0 + 11.0)


def test_all_hubs_offline_is_not_a_double_sale() -> None:
    """Dev-stack regression (2026-09-26): K2 rose 0 -> 750 kWh while every hub was OFFLINE. Reservations
    within rated capability are never K2, whatever the live state -- that loss is a K13 shortfall."""
    hub_rows: list[_ROW] = [
        ("bank-000", 600.0, 0.0, 39.2, 7.84, 11.0, 0.9487, 0.9487, 0.0, "offline", 1) for _ in range(10)
    ]
    rows = [("bank-000", NOW, NOW + timedelta(minutes=15), 110.0)]  # the full rated 10 x 11 kW
    assert checks.find_double_sold(rows, checks.compute_bank_rated_capabilities_kw(hub_rows)) == []


def test_a_true_double_reservation_is_a_double_sale() -> None:
    """Two obligations reserving the same bank/interval beyond its rated capability: K2 > 0."""
    hub_rows: list[_ROW] = [
        ("bank-000", 600.0, 0.0, 39.2, 7.84, 11.0, 0.9487, 0.9487, 39.2, "online", 1) for _ in range(10)
    ]
    rows = [("bank-000", NOW, NOW + timedelta(minutes=15), 110.0 + 80.0)]  # 110 kW + a second 80 kW sale
    violations = checks.find_double_sold(rows, checks.compute_bank_rated_capabilities_kw(hub_rows))
    assert len(violations) == 1
    assert violations[0].detail["excess_kw"] == pytest.approx(80.0)
    assert violations[0].magnitude == pytest.approx(20.0)  # 80 kW x 0.25 h


def test_never_anchored_is_tolerated_only_within_the_grace_window() -> None:
    grace_until = NOW + timedelta(minutes=30)
    assert (
        checks.find_anchor_staleness_violation(
            last_published_at=None, now=NOW, max_age_s=1800.0, never_anchored_grace_until=grace_until
        )
        is None
    )
    late = checks.find_anchor_staleness_violation(
        last_published_at=None, now=grace_until, max_age_s=1800.0, never_anchored_grace_until=grace_until
    )
    assert late is not None and late.dedupe_key == "never_anchored"
    # A stale anchor is never excused by the grace: the grace only covers "no anchor yet".
    stale = checks.find_anchor_staleness_violation(
        last_published_at=NOW - timedelta(hours=2),
        now=NOW,
        max_age_s=1800.0,
        never_anchored_grace_until=grace_until,
    )
    assert stale is not None


# --- AS capacity hold: held / deployed / netted ---------------------------------------------------------


def _hold(
    obligation_id: str,
    kw: float,
    *,
    service_type: str = "ERCOT_AS",
    duration_minutes: int | None = 60,
    deployment_end: datetime | None = None,
    bank_id: str = "bank-000",
) -> checks.HoldReservation:
    return checks.HoldReservation(
        obligation_id=obligation_id,
        bank_id=bank_id,
        kw=kw,
        interval_start=NOW,
        interval_end=NOW + timedelta(minutes=15),
        service_type=service_type,
        duration_minutes=duration_minutes,
        deployment_id="dep-1" if deployment_end is not None else None,
        deployment_end=deployment_end,
    )


def _bank(stored_above_reserve_kwh: float, *, health: str = "OK") -> dict[str, list[HubSnapshot]]:
    """One hub with `stored_above_reserve_kwh` above a 10 kWh reserve; e_kwh 100 (1 kWh G-01 margin)."""
    hub = HubSnapshot(
        hub_id="hub-1",
        bank_id="bank-000",
        free_discharge_kw=0.0,
        health=health,  # type: ignore[arg-type]
        soc_kwh=10.0 + stored_above_reserve_kwh,
        reserve_kwh=10.0,
        e_kwh=100.0,
        eta_d=1.0,
    )
    return {"bank-000": [hub]}


def test_a_held_award_short_of_its_full_deployment_is_flagged() -> None:
    """ECRS 50 kW x 60 min held (never deployed) needs 50 kWh; 40 kWh above reserve (less the margin)."""
    violations = checks.find_as_hold_violations([_hold("ob-1", 50.0)], _bank(40.0), now=NOW)
    assert len(violations) == 1
    assert violations[0].detail["state"] == "HELD"
    assert violations[0].dedupe_key.startswith("held|ob-1|")
    assert violations[0].detail["required_kwh"] == pytest.approx(50.0)


def test_a_held_award_with_enough_energy_is_clean() -> None:
    assert checks.find_as_hold_violations([_hold("ob-1", 50.0)], _bank(60.0), now=NOW) == []


def test_mid_deployment_only_the_remaining_window_is_required() -> None:
    """Deployed 50 kW with 20 min left needs ~16.7 kWh, not the full 50 kWh: 30 kWh is enough."""
    res = _hold("ob-1", 50.0, deployment_end=NOW + timedelta(minutes=20))
    assert checks.find_as_hold_violations([res], _bank(30.0), now=NOW) == []
    # ...and a deployment that has drained below even its remaining need is flagged, keyed by deployment.
    (short,) = checks.find_as_hold_violations([res], _bank(10.0), now=NOW)
    assert short.detail["state"] == "DEPLOYED" and short.dedupe_key == "dep-1"


def test_another_reservation_on_the_same_bank_cannot_mask_a_shortfall() -> None:
    """60 kWh above reserve covers the 50 kWh hold alone, but a firm 200 kW reservation for the rest of
    the 15-min interval claims 50 kWh of it: the hold is short."""
    reservations = [_hold("ob-1", 50.0), _hold("ob-firm", 200.0, service_type="ERCOT_ENERGY")]
    violations = checks.find_as_hold_violations(reservations, _bank(60.0), now=NOW)
    assert [v.scope["obligation_id"] for v in violations] == ["ob-1"]


def test_energy_on_unhealthy_hubs_does_not_count() -> None:
    assert (
        len(checks.find_as_hold_violations([_hold("ob-1", 50.0)], _bank(500.0, health="STALE"), now=NOW)) == 1
    )
