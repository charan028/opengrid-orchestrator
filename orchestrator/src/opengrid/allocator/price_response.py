"""Free-headroom price-responsive schedule (02a S5.1 S6, S5.5): uncommitted headroom left after the
lexicographic tiers and substitution is offered to `ERCOT_ENERGY` when price clears a threshold,
with a 5-minute dwell (no mode switch inside 5 min of the last one) and $5/MWh hysteresis (the
switch threshold is offset +-$5/MWh depending on the current mode) -- `03` S8.6.5's rule, ported
unchanged. This is what stops the allocator from flapping a hub's setpoint on every 2 s price tick.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from opengrid.allocator.models import DwellState

_DWELL = timedelta(minutes=5)
_HYSTERESIS_USD_PER_MWH = 5.0


def price_responsive_schedule(
    headroom_kw: float,
    price_usd_per_mwh: float,
    threshold_usd_per_mwh: float,
    dwell_state: DwellState,
    now: datetime,
) -> tuple[float, DwellState]:
    """Decide how much of `headroom_kw` to schedule to the market this cycle.

    Switching from LOW to HIGH needs price >= threshold + hysteresis/2; switching back needs
    price <= threshold - hysteresis/2. Either switch is refused if the dwell window since the last
    switch has not elapsed, in which case the current mode holds. Returns (scheduled_kw, new_state).
    """
    half_band = _HYSTERESIS_USD_PER_MWH / 2.0
    upper = threshold_usd_per_mwh + half_band
    lower = threshold_usd_per_mwh - half_band

    dwell_elapsed = dwell_state.last_switch_at is None or (now - dwell_state.last_switch_at) >= _DWELL

    new_mode = dwell_state.mode
    if dwell_elapsed:
        if dwell_state.mode == "LOW" and price_usd_per_mwh >= upper:
            new_mode = "HIGH"
        elif dwell_state.mode == "HIGH" and price_usd_per_mwh <= lower:
            new_mode = "LOW"

    if new_mode != dwell_state.mode:
        new_state = DwellState(mode=new_mode, last_switch_at=now)
    else:
        new_state = replace(dwell_state)

    scheduled_kw = headroom_kw if new_mode == "HIGH" else 0.0
    return scheduled_kw, new_state
