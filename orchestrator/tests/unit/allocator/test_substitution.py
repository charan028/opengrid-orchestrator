"""TS-05: substitution within an obligation on hub health loss (02a S5.3). K13's "substitution
always allowed, never a reallocation to a different obligation" invariant.
"""

from __future__ import annotations

from opengrid.allocator import reasons
from opengrid.allocator.models import HubSnapshot
from opengrid.allocator.substitution import realize_obligation


def _hub(hub_id: str, kw: float, health: str = "OK") -> HubSnapshot:
    return HubSnapshot(hub_id=hub_id, bank_id="b1", free_discharge_kw=kw, health=health)


def test_ts_05_20_substitutes_unhealthy_hub_keeps_full_delivery() -> None:
    hubs = (_hub("h1", 10.0, health="FAULT"), _hub("h2", 10.0), _hub("h3", 10.0))
    result = realize_obligation("o1", "b1", hubs, needed_kw=15.0)
    assert sum(result.per_hub_kw.values()) == 15.0
    assert result.per_hub_kw.get("h1", 0.0) == 0.0  # faulted hub gets nothing
    assert result.shortfall is None
    assert result.event is not None
    assert result.event.from_hub_ids == ("h1",)
    assert set(result.event.to_hub_ids) <= {"h2", "h3"}


def test_ts_05_21_no_substitute_reports_shortfall_against_same_obligation() -> None:
    hubs = (_hub("h1", 10.0, health="FAULT"), _hub("h2", 5.0))
    result = realize_obligation("o1", "b1", hubs, needed_kw=15.0)
    assert sum(result.per_hub_kw.values()) == 5.0
    assert result.shortfall is not None
    assert result.shortfall.obligation_id == "o1"
    assert result.shortfall.shortfall_kw == 10.0
    assert result.shortfall.reason_code == reasons.R_COMMIT_LOCK_INFEASIBLE


def test_ts_05_22_all_healthy_no_substitution_event() -> None:
    hubs = (_hub("h1", 10.0), _hub("h2", 10.0))
    result = realize_obligation("o1", "b1", hubs, needed_kw=10.0)
    assert result.event is None
    assert result.shortfall is None


def test_ts_05_23_zero_needed_is_a_no_op() -> None:
    hubs = (_hub("h1", 10.0, health="FAULT"),)
    result = realize_obligation("o1", "b1", hubs, needed_kw=0.0)
    assert result.per_hub_kw == {}
    assert result.shortfall is None
    assert result.event is None


def test_ts_05_24_never_exceeds_healthy_capacity() -> None:
    hubs = (_hub("h1", 3.0), _hub("h2", 4.0, health="LAGGING"))
    result = realize_obligation("o1", "b1", hubs, needed_kw=100.0)
    assert sum(result.per_hub_kw.values()) == 3.0
    assert result.shortfall is not None
    assert result.shortfall.shortfall_kw == 97.0
