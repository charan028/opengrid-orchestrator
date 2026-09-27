"""Pure call checks (`opengrid.calls.rules`) and the engine's called-kW scaling (D-33)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from hypothesis import given
from hypothesis import strategies as st

from opengrid.calls import CallKind, CallOrigin, CallRequest
from opengrid.calls import rules as r
from opengrid.engine.gateways import called_kw_scale

NOW = datetime(2026, 9, 26, 21, 40, tzinfo=UTC)


def _req(**kw) -> CallRequest:
    return CallRequest(
        **{"origin": CallOrigin.UTILITY, "principal": "p", "reason": "x", "duration_minutes": 10, **kw}
    )


def test_kinds() -> None:
    assert r.deployment_kind("ERCOT_AS", None) is CallKind.AS
    assert r.deployment_kind("REGULATED_CAPACITY", "tolling") is CallKind.UTILITY_CALL
    assert r.deployment_kind("REGULATED_CAPACITY", "FIRM") is None


@given(st.floats(min_value=0.0, max_value=1e6, allow_nan=False))
def test_property_any_non_negative_kw_is_refused_discharge_only(kw: float) -> None:
    refusal = r.check_request(_req(requested_kw=kw), NOW, NOW + timedelta(minutes=10), NOW)
    assert refusal is not None and refusal.reason_code == r.R_CHARGE_REFUSED


@given(st.floats(min_value=-1e6, max_value=-1e-3, allow_nan=False))
def test_property_any_negative_kw_passes_the_sign_check(kw: float) -> None:
    assert r.check_request(_req(requested_kw=kw), NOW, NOW + timedelta(minutes=10), NOW) is None


def test_a_late_start_begins_now_and_keeps_its_own_end() -> None:
    start, end = r.call_window(_req(start_at=NOW - timedelta(minutes=2), duration_minutes=10), NOW)
    assert start == NOW and end == NOW + timedelta(minutes=8)
    start, end = r.call_window(_req(duration_minutes=None, end_at=NOW + timedelta(minutes=5)), NOW)
    assert (start, end) == (NOW, NOW + timedelta(minutes=5))


def test_status_for_stored_refusals() -> None:
    assert r.status_for(r.R_RATE_LIMIT) == 429
    assert r.status_for(r.R_NOT_FOUND) == 404
    assert r.status_for(r.R_OVERLAP) == 409
    assert r.status_for(None) == 409


def test_called_kw_scale_caps_a_called_obligation_across_its_banks() -> None:
    called, full, held = str(uuid4()), str(uuid4()), str(uuid4())
    rows = [
        # obligation, bank, amount, service, tier, value, state, as_deployed, duration, end, deploy_kw
        (called, "bank-040", 600.0, "REGULATED_CAPACITY", "T1", 0, "COMMITTED", True, 90, None, 450.0),
        (called, "bank-041", 300.0, "REGULATED_CAPACITY", "T1", 0, "COMMITTED", True, 90, None, 450.0),
        (full, "bank-042", 500.0, "ERCOT_AS", "T1", 0, "COMMITTED", True, 60, None, None),
        (held, "bank-043", 500.0, "REGULATED_CAPACITY", "T1", 0, "COMMITTED", False, 90, None, 100.0),
    ]
    assert called_kw_scale(rows) == {called: 0.5}
    assert called_kw_scale([(*rows[0][:10], 900.0)]) == {}  # asking for >= committed keeps committed
    assert called_kw_scale([rows[2][:10]]) == {}  # pre-0047 row shape (no deploy_kw column)
