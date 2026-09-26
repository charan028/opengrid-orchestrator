"""Water-filling with stickiness (02a S5.5): p_i = min(A_i, theta * w_i).

w_i = E_free_i * tau_i * (1 + 0.2 * served_last_cycle). Solved by sorting A_i / w_i ascending
(O(n log n)) and finding the water level theta such that sum(p_i) hits the target as closely as
possible without exceeding any hub's own capability A_i (K1/K4: never propose more than a hub's
reserve-safe free capability).

Deterministic tie-breaks: hubs are sorted by (A_i/w_i, hub_id) so equal ratios resolve the same way
every time, regardless of input ordering.
"""

from __future__ import annotations

import numpy as np

from opengrid.allocator.models import HubSnapshot

_STICKINESS_DEFAULT = 0.2
_EPS = 1e-9


def hub_weight(hub: HubSnapshot, stickiness: float = _STICKINESS_DEFAULT) -> float:
    """w_i = E_free_i * tau_i * (1 + stickiness * served_last_cycle)."""
    bonus = 1.0 + stickiness if hub.served_last_cycle else 1.0
    return hub.free_discharge_kw * hub.tau * bonus


def water_fill(
    hubs: tuple[HubSnapshot, ...],
    target_kw: float,
    *,
    stickiness: float = _STICKINESS_DEFAULT,
) -> dict[str, float]:
    """Split `target_kw` across `hubs` by water-filling on weighted capability.

    Returns a mapping hub_id -> granted_kw, every value in [0, hub.free_discharge_kw]. If the sum of
    all hubs' free_discharge_kw is less than `target_kw`, every hub is granted its full capability
    (the caller is responsible for reporting the residual as a shortfall -- this function never
    invents capacity, K1/K4).

    ALLOC-07: if every hub's weight (`hub_weight`) is zero (e.g. `tau=0` for all of them), water-
    filling on a zero weight is undefined, so `target_kw` is split pro-rata by each hub's own
    `free_discharge_kw` instead of granting everyone zero.
    """
    if not hubs or target_kw <= 0:
        return {h.hub_id: 0.0 for h in hubs}

    order = sorted(hubs, key=lambda h: h.hub_id)
    caps = np.array([h.free_discharge_kw for h in order], dtype=np.float64)
    weights = np.array([hub_weight(h, stickiness) for h in order], dtype=np.float64)
    ids = [h.hub_id for h in order]

    total_cap = float(caps.sum())
    if target_kw >= total_cap - _EPS:
        return dict(zip(ids, caps.tolist(), strict=True))

    if not np.any(weights > _EPS):
        # ALLOC-07: all-zero weights (e.g. every hub has tau=0 or zero free capability priced the
        # same) -- water-filling on a zero weight is undefined (theta*0 == 0 for any theta), so fall
        # back to splitting `target_kw` pro-rata by each hub's own capability instead of granting
        # everyone zero and reporting a false shortfall.
        if total_cap <= _EPS:
            return {h.hub_id: 0.0 for h in hubs}
        granted = caps * (target_kw / total_cap)
        return dict(zip(ids, granted.tolist(), strict=True))

    theta = _solve_water_level(caps, weights, target_kw)
    granted = np.minimum(caps, theta * weights)
    granted = np.clip(granted, 0.0, caps)

    # Floating-point residual correction: nudge the largest-headroom hub so the sum matches the
    # target exactly (deterministic: ties broken by the same sorted hub_id order).
    residual = target_kw - float(granted.sum())
    if abs(residual) > _EPS:
        headroom = caps - granted
        idx = int(np.argmax(headroom)) if residual > 0 else int(np.argmax(granted))
        adjust = min(residual, float(headroom[idx])) if residual > 0 else max(residual, -float(granted[idx]))
        granted[idx] += adjust

    return dict(zip(ids, granted.tolist(), strict=True))


def _solve_water_level(caps: np.ndarray, weights: np.ndarray, target_kw: float) -> float:
    """Classic water-filling level search: process hubs by ascending saturation threshold
    (cap_i / w_i); a hub whose threshold is below the current candidate water level is pinned to
    its own cap and removed from the pool, until the remaining pool's level no longer exceeds the
    next hub's threshold. O(n log n) via one sort.
    """
    active = weights > _EPS
    if not np.any(active):
        return 0.0

    threshold = np.where(active, caps / np.where(active, weights, 1.0), np.inf)
    order = np.argsort(threshold, kind="stable")

    remaining_target = target_kw
    pool_weight = float(weights[active].sum())
    for idx in order:
        if not active[idx]:
            continue
        if pool_weight <= _EPS:
            return 0.0
        theta_candidate = remaining_target / pool_weight
        if theta_candidate <= threshold[idx] + _EPS:
            return max(theta_candidate, 0.0)
        # This hub's cap binds before the pool reaches equilibrium: pin it and continue.
        remaining_target -= caps[idx]
        pool_weight -= weights[idx]
    return max(remaining_target / pool_weight, 0.0) if pool_weight > _EPS else 0.0
