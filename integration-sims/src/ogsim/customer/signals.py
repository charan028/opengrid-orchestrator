"""ogsim.customer.signals -- builds the DATA_CENTER site-meter and PIPELINE_AC
corridor-current wire messages (`interfaces/mqtt/customer_site_meter.schema.json`,
`interfaces/mqtt/pipeline_corridor_current.schema.json`), the closed-loop site
measurements 06-service-profiles-and-power-quality.md requires for those two service
profiles.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from ogsim.common.scenario import utc_timestamp
from ogsim.customer.config import CustomerSiteSpec

NOMINAL_PHASE_VOLTAGE_V = 277.0  # 480Y/277V three-phase service, typical commercial/DC site
NOMINAL_FREQ_HZ = 60.0
NOMINAL_PF = 0.98


def site_meter_topic(site: CustomerSiteSpec) -> str:
    return f"site/{site.customer_id}/{site.site_id}/meter"


def corridor_current_topic(site: CustomerSiteSpec) -> str:
    return f"corridor/{site.customer_id}/{site.corridor_id}/current"


def build_site_meter_message(
    site: CustomerSiteSpec, now: float, p_kw: float, rng: np.random.Generator, quality: str = "GOOD"
) -> dict[str, Any]:
    """Builds a CustomerSiteMeter reading at the point of common coupling for a DATA_CENTER
    site: signed real power (positive = import from the grid), reactive power at
    `NOMINAL_PF`, per-phase RMS voltage/current with small noise, and frequency/THD near
    nominal."""
    q_kvar = p_kw * math.tan(math.acos(NOMINAL_PF))
    v_phases = [NOMINAL_PHASE_VOLTAGE_V + float(rng.normal(0.0, 0.5)) for _ in range(3)]
    per_phase_kw = p_kw / 3.0
    i_phases = [
        abs(per_phase_kw * 1000.0 / (v_phases[i] * NOMINAL_PF)) + abs(float(rng.normal(0.0, 0.5)))
        for i in range(3)
    ]
    return {
        "site_id": site.site_id,
        "customer_id": site.customer_id,
        "ts": utc_timestamp(now),
        "p_kw": round(p_kw, 3),
        "q_kvar": round(q_kvar, 3),
        "v_rms_a_v": round(v_phases[0], 2),
        "v_rms_b_v": round(v_phases[1], 2),
        "v_rms_c_v": round(v_phases[2], 2),
        "i_rms_a_a": round(i_phases[0], 2),
        "i_rms_b_a": round(i_phases[1], 2),
        "i_rms_c_a": round(i_phases[2], 2),
        "freq_hz": round(NOMINAL_FREQ_HZ + float(rng.normal(0.0, 0.01)), 4),
        "pf": NOMINAL_PF,
        "thd_v_pct": round(abs(float(rng.normal(1.5, 0.3))), 3),
        "thd_i_pct": round(abs(float(rng.normal(3.0, 0.5))), 3),
        "quality": quality,
    }


def build_corridor_current_message(
    site: CustomerSiteSpec, now: float, i_ac_a: float, rng: np.random.Generator, quality: str = "GOOD"
) -> dict[str, Any]:
    """Builds a PipelineCorridorCurrent reading: the AC current induced on the pipe versus
    the customer's mitigation limit (PIPELINE_AC service profile closed-loop signal)."""
    return {
        "corridor_id": site.corridor_id,
        "line_id": site.line_id,
        "customer_id": site.customer_id,
        "ts": utc_timestamp(now),
        "i_ac_a": round(max(0.0, i_ac_a + float(rng.normal(0.0, 0.1))), 3),
        "limit_a": site.limit_a,
        "quality": quality,
    }


__all__ = [
    "NOMINAL_FREQ_HZ",
    "NOMINAL_PF",
    "NOMINAL_PHASE_VOLTAGE_V",
    "build_corridor_current_message",
    "build_site_meter_message",
    "corridor_current_topic",
    "site_meter_topic",
]
