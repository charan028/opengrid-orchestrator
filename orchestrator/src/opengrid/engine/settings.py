"""og-engine dispatch settings, read once from config at start-up (02b S1.4).

| Key | Default | Meaning |
|---|---|---|
| `[contracts.activation].data_center` | false | 03 S2.7 gate: DATA_CENTER/PIPELINE_AC admission |
| `[site_ingest].enabled` | false | route `site/#` and `corridor/#` MQTT to `opengrid.site_ingest` |
| `[allocator.closed_loop].enabled` | false | run the DATA_CENTER/PIPELINE_AC controllers in the cycle |
| `[allocator].enforce_territory` | true | K15: obligations only on banks their market may use |
| `[allocator].wear_usd_per_kwh` | 0.03 | 09 D8 wear rate in the stored-energy threshold fallback |
| `[allocator.flow_limits]` | on | 09 S1.9 F2/F3 caps where the 0029 registry (or config) has data; F1 always |
| `[allocator].propose_timeout_s` | 2.0 | bound on the whole propose phase; banks cut off hold (`engine.propose_guard`) |
| `[allocator].guardian_check_timeout_s` | 0.5 | bound on the guardian heartbeat read; a timeout = unavailable |

The closed-loop and site-ingest switches are the owner's to turn on; both default off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from opengrid.allocator.models import FlowLimits
from opengrid.engine.gateways import DEFAULT_WEAR_USD_PER_KWH
from opengrid.engine.propose_guard import DEFAULT_GUARDIAN_CHECK_TIMEOUT_S, DEFAULT_PROPOSE_TIMEOUT_S
from opengrid.engine.veto import DEFAULT_EXCLUDE_CYCLES, DEFAULT_VERDICT_WAIT_S
from opengrid.platform.config import Config

#: `[guardian.flow].default_export_limit_kw`'s default (guardian/config.py), shared with the allocator's F2.
GUARDIAN_DEFAULT_EXPORT_LIMIT_KW = 20.0
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
    #: K4 veto fail-safe (`engine.veto`): on/off, cycles a vetoed hub stays out, wait for verdicts (s).
    veto_retry_enabled: bool = False  # off until proven in prod (H1/H2); the owner enables it
    veto_exclude_cycles: int = DEFAULT_EXCLUDE_CYCLES
    verdict_wait_s: float = DEFAULT_VERDICT_WAIT_S
    #: How often the K15 market model is rebuilt (re-zoning, market changes).
    market_refresh_s: float = 60.0
    #: Bounded guardian hand-off (`engine.propose_guard`): the whole propose phase, and the heartbeat read.
    propose_timeout_s: float = DEFAULT_PROPOSE_TIMEOUT_S
    guardian_check_timeout_s: float = DEFAULT_GUARDIAN_CHECK_TIMEOUT_S


def dispatch_settings(cfg: Config) -> DispatchSettings:
    flow: Any = cfg.get("allocator.flow_limits", {}) or {}
    pac: Any = cfg.get("allocator.closed_loop.pipeline_ac", {}) or {}
    # G-26's premise default, read from the guardian's own key so allocator and guardian agree (review R3):
    # a hub with no og.hub.export_limit_kw is capped at it on both sides. "unknown" = no default.
    raw_export = cfg.get("guardian.flow.default_export_limit_kw", GUARDIAN_DEFAULT_EXPORT_LIMIT_KW)
    default_export = None if raw_export in (None, "unknown") else float(str(raw_export))
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
        veto_retry_enabled=bool(cfg.get("allocator.veto_retry.enabled", False)),
        veto_exclude_cycles=int(cfg.get("allocator.veto_retry.exclude_cycles", DEFAULT_EXCLUDE_CYCLES)),
        verdict_wait_s=float(cfg.get("allocator.veto_retry.wait_s", DEFAULT_VERDICT_WAIT_S)),
        market_refresh_s=float(cfg.get("allocator.market_refresh_s", 60.0)),
        propose_timeout_s=float(cfg.get("allocator.propose_timeout_s", DEFAULT_PROPOSE_TIMEOUT_S)),
        guardian_check_timeout_s=float(
            cfg.get("allocator.guardian_check_timeout_s", DEFAULT_GUARDIAN_CHECK_TIMEOUT_S)
        ),
        pipeline_ac=PipelineAcDefaults(
            line_kv=float(pac.get("line_kv", 138.0)) if isinstance(pac, dict) else 138.0,
            power_factor=float(pac.get("power_factor", 0.95)) if isinstance(pac, dict) else 0.95,
            shift_factor=float(shift) if shift is not None else None,
            direction=int(pac.get("direction", 1)) if isinstance(pac, dict) else 1,
            freshness_s=float(pac.get("freshness_s", 4.0)) if isinstance(pac, dict) else 4.0,
        ),
    )
