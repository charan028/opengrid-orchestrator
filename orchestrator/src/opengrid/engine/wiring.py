"""og-engine start-up builders for the dispatch additions of 2026-09-26: the K15 market model, the M1 table,
the optimizer's stored-energy value reader and the cycle extras (closed loop, PQ context, ladder, flow
limits). Each degrades to a documented safe default and logs instead of failing the process start."""

from __future__ import annotations

import logging
import tomllib
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from opengrid.engine import pq_eligibility
from opengrid.engine.closed_loop import ClosedLoopRunner, default_site_signals, pg_spec_loader
from opengrid.engine.dispatch_extras import EngineCycleExtras
from opengrid.engine.flow_topology import FlowTopology
from opengrid.engine.gateways import StoredEnergyValueReader, m1_usd_per_mwh_by_zone
from opengrid.engine.pq_ladder import PqLadderExecutor, TracePort, calibration_requester
from opengrid.engine.settings import DispatchSettings
from opengrid.market.model import MarketModel, load_market_model

logger = logging.getLogger(__name__)


def load_fleet_market_model(fleet_module: Any) -> MarketModel | None:
    """The K15 market model over the fleet twin's banks. `None` (logged) when `tdsp_tariffs.toml` cannot
    be read: every bank's territory is then unknown, and with territory enforced nothing that needs it is
    dispatched (fail closed)."""
    try:
        banks = [(b, fleet_module.bank_zone(b)) for b in fleet_module.known_bank_ids()]
        return load_market_model(banks=banks)
    except Exception:
        logger.exception("market model unavailable; bank territories unknown (K15 fails closed)")
        return None


def load_m1_by_zone() -> dict[str, float] | None:
    """M1 per load zone from the tariff file; `None` keeps the gateway's built-in table."""
    try:
        return m1_usd_per_mwh_by_zone(datetime.now(UTC).date())
    except Exception:
        logger.exception("TDSP tariffs unreadable; the built-in M1 table prices the threshold fallback")
        return None


def stored_energy_value_reader() -> StoredEnergyValueReader:
    """The optimizer's in-process read API (`opengrid.selector.energy_value.discharge_threshold_usd_per_mwh`,
    the latest LP plan this og-engine process solved): per bank, the break-even price for discharging one AC
    MWh of headroom now, wear included. A bank with no covering plan is absent (fallback threshold)."""
    from opengrid.selector.energy_value import discharge_threshold_usd_per_mwh

    async def _read(bank_ids: Sequence[str], now: datetime) -> dict[str, float]:
        values: dict[str, float] = {}
        for bank_id in bank_ids:
            value = discharge_threshold_usd_per_mwh(bank_id, now)
            if value is not None:
                values[bank_id] = value
        return values

    return _read


def load_dc_profile() -> dict[str, Any] | None:
    path = pq_eligibility.PROFILES_DIR / "data_center.toml"
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except OSError:
        logger.warning("data_center.toml unreadable; DATA_CENTER controller uses built-in defaults")
        return None


def build_cycle_extras(
    pool: AsyncConnectionPool,
    trace: TracePort,
    settings: DispatchSettings,
    *,
    asset_service: Any,
    set_at_risk: Callable[..., Awaitable[object]],
) -> EngineCycleExtras:
    """The engine's cycle extras. The closed-loop runner (and so the S5.4 ladder fed by its PCC readings)
    exists when site ingest or closed-loop control is on; control itself only with
    `[allocator.closed_loop].enabled`."""
    runner: ClosedLoopRunner | None = None
    ladder: PqLadderExecutor | None = None
    if settings.site_ingest_enabled or settings.closed_loop_enabled:
        runner = ClosedLoopRunner(
            pg_spec_loader(pool),
            settings,
            dc_profile=load_dc_profile(),
            signals=default_site_signals(),
            control_enabled=settings.closed_loop_enabled,
        )

        async def _flag(oid: UUID, at_risk: bool, reason: str, payload: dict[str, object]) -> object:
            return await set_at_risk(oid, at_risk, reason_code=reason, payload=payload)

        ladder = PqLadderExecutor(
            trace,
            set_at_risk=_flag,
            candidate_of=pq_eligibility.candidate,
            request_calibration=calibration_requester(asset_service),
        )
    return EngineCycleExtras(
        trace,
        pq_context=pq_eligibility.dispatch_context,
        closed_loop=runner,
        ladder=ladder,
        enforce_territory=settings.enforce_territory,
        flow_limits=settings.flow_limits,
        flow_topology=FlowTopology(pool, settings.flow_limits),
    )
