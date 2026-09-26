"""og-engine dispatch settings, read once from config at start-up (02b S1.4).

| Key | Default | Meaning |
|---|---|---|
| `[contracts.activation].data_center` | false | 03 S2.7 gate: DATA_CENTER/PIPELINE_AC admission |
| `[site_ingest].enabled` | false | route `site/#` and `corridor/#` MQTT to `opengrid.site_ingest` |
| `[allocator.closed_loop].enabled` | false | run the DATA_CENTER/PIPELINE_AC controllers in the cycle |
| `[allocator].enforce_territory` | true | K15: obligations only on banks their market may use |
| `[allocator].wear_usd_per_kwh` | 0.03 | 09 D8 wear rate in the stored-energy threshold fallback |
| `[allocator.flow_limits]` | on | 09 S1.9 F2/F3 caps where the 0029 registry (or config) has data; F1 always |

The closed-loop and site-ingest switches are the owner's to turn on; both default off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from opengrid.allocator.models import FlowLimits
from opengrid.engine.gateways import DEFAULT_WEAR_USD_PER_KWH
from opengrid.platform.config import Config

#: DATA_CENTER site meters are 480Y/277 V services (06 S4.b); `[allocator.closed_loop].site_nominal_v`.
DEFAULT_SITE_NOMINAL_V = 277.0


def _float_table(raw: object) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    return {str(k): float(v) for k, v in raw.items()}


@dataclass(frozen=True, slots=True)
class PipelineAcDefaults:
    """PIPELINE_AC corridor model (06 S4.a) until contracts carry it. No shift factor: open loop."""

    line_kv: float = 138.0
    power_factor: float = 0.95
    shift_factor: float | None = None
    direction: int = 1
    freshness_s: float = 4.0


@dataclass(frozen=True, slots=True)
class DispatchSettings:
    data_center_activation: bool = False
    site_ingest_enabled: bool = False
    closed_loop_enabled: bool = False
    enforce_territory: bool = True
    wear_usd_per_kwh: float = DEFAULT_WEAR_USD_PER_KWH
    flow_limits: FlowLimits = field(default_factory=lambda: FlowLimits(enabled=True))
    site_nominal_v: float = DEFAULT_SITE_NOMINAL_V
    #: DATA_CENTER site-meter import to hold, per site id (`None` for a site: feed-forward only).
    dc_target_import_kw: dict[str, float] = field(default_factory=dict)
    pipeline_ac: PipelineAcDefaults = field(default_factory=PipelineAcDefaults)


def dispatch_settings(cfg: Config) -> DispatchSettings:
    flow: Any = cfg.get("allocator.flow_limits", {}) or {}
    pac: Any = cfg.get("allocator.closed_loop.pipeline_ac", {}) or {}
    default_export = flow.get("default_export_limit_kw") if isinstance(flow, dict) else None
    shift = pac.get("shift_factor") if isinstance(pac, dict) else None
    return DispatchSettings(
        data_center_activation=bool(cfg.get("contracts.activation.data_center", False)),
        site_ingest_enabled=bool(cfg.get("site_ingest.enabled", False)),
        closed_loop_enabled=bool(cfg.get("allocator.closed_loop.enabled", False)),
        enforce_territory=bool(cfg.get("allocator.enforce_territory", True)),
        wear_usd_per_kwh=float(cfg.get("allocator.wear_usd_per_kwh", DEFAULT_WEAR_USD_PER_KWH)),
        flow_limits=FlowLimits(
            enabled=bool(flow.get("enabled", True)) if isinstance(flow, dict) else True,
            default_export_limit_kw=float(default_export) if default_export is not None else None,
            xfmr_kva=_float_table(flow.get("xfmr_kva") if isinstance(flow, dict) else None),
            feeder_budget_kw=_float_table(flow.get("feeder_budget_kw") if isinstance(flow, dict) else None),
            substation_budget_kw=_float_table(
                flow.get("substation_budget_kw") if isinstance(flow, dict) else None
            ),
        ),
        site_nominal_v=float(cfg.get("allocator.closed_loop.site_nominal_v", DEFAULT_SITE_NOMINAL_V)),
        dc_target_import_kw=_float_table(cfg.get("allocator.closed_loop.data_center.target_import_kw")),
        pipeline_ac=PipelineAcDefaults(
            line_kv=float(pac.get("line_kv", 138.0)) if isinstance(pac, dict) else 138.0,
            power_factor=float(pac.get("power_factor", 0.95)) if isinstance(pac, dict) else 0.95,
            shift_factor=float(shift) if shift is not None else None,
            direction=int(pac.get("direction", 1)) if isinstance(pac, dict) else 1,
            freshness_s=float(pac.get("freshness_s", 4.0)) if isinstance(pac, dict) else 4.0,
        ),
    )
