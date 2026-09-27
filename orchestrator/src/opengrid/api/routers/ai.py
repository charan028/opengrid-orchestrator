"""AI copilot routes (issue #26): advisory answers over live state, and the agent's own status.

**Read-only and advisory.** Both routes depend on `require_viewer`; nothing here can command, approve or
release anything. The copilot's snapshot is assembled in-process through the console's own read-only GET
handlers (`GET /og/api/health`, `GET /og/api/dispatch/opportunities`) acting as the dedicated
`og-ai-agent` viewer identity, over a store view that exposes only the read methods those handlers use.
The agent itself never sees the store, the pool or a table: it gets the handlers' JSON.

Fleet questions ("how many hubs below 30% charge in LZ_NORTH") go to a read-only fleet tool built here
over the Fleet table's own filter and aggregate layer (`fleet_search.fleet_summary`), through a view of
the store that runs single SELECT statements only. The tool returns counts, totals and a few equipment
rows (no position, no customer field); only those query results can reach a model.

Every interaction is written to its own `ai:<user>` trace stream using the existing trace API (no
migration: the stream is new, the decision type is not). If that write fails, the operator is told the
assistant is unavailable rather than shown an untraced answer.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from opengrid import ai_agent
from opengrid.ai_agent import fleet as ai_agent_fleet
from opengrid.ai_agent.gateway import ScreeningHealth
from opengrid.api.auth import Identity, Role, require_viewer
from opengrid.api.deps import get_config, get_store, get_trace_store
from opengrid.api.routers import dispatch as dispatch_routes
from opengrid.api.routers import fleet_search
from opengrid.api.routers import health as health_routes
from opengrid.api.store import StoreProtocol
from opengrid.health.model import AlertFinding
from opengrid.health.queries import clear_alert, fetch_open_alerts, raise_alert
from opengrid.platform.config import Config
from opengrid.trace.store import TraceStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/og/api/ai", tags=["ai"])

#: The service identity the snapshot reads run as: a viewer, so it can never pass `require_operator`.
AI_AGENT_IDENTITY = Identity("og-ai-agent", Role.VIEWER)

#: The only store methods the snapshot's GET handlers may reach. Every one is a SELECT.
READ_METHODS: frozenset[str] = frozenset(
    {
        "count_active_commitments",
        "feed_statuses",
        "health_snapshot",
        "hub_health_summary",
        "list_alerts",
        "list_hubs",
        "list_obligations",
        "profitability_summary",
    }
)

#: How much of the board the copilot may look at in one answer. A cap, not a page size.
_MAX_OBLIGATIONS = 60
_MAX_ALERTS = 20


class ReadOnlyStore:
    """A view of the API store that exposes only `READ_METHODS`. Anything else -- every write, ack or
    command method -- is an `AttributeError`, so a handler change that starts writing fails loudly here
    instead of writing as the copilot."""

    __slots__ = ("_store",)

    def __init__(self, store: StoreProtocol) -> None:
        self._store = store

    def __getattr__(self, name: str) -> Any:
        if name not in READ_METHODS:
            raise AttributeError(f"the copilot's read-only store view has no {name!r}")
        return getattr(self._store, name)


class SelectOnlyRows:
    """The fleet read helper, restricted to single SELECT statements (defence in depth: every statement
    the fleet tool runs is built by `fleet_search` from fixed fragments, and this refuses anything else)."""

    __slots__ = ("_store",)

    def __init__(self, store: fleet_search.FleetRowsStore) -> None:
        self._store = store

    async def fleet_rows(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        if not sql.lstrip().upper().startswith("SELECT") or ";" in sql:
            raise PermissionError("the copilot's fleet tool runs single SELECT statements only")
        return await self._store.fleet_rows(sql, params)


#: The owner's asset words -> the Fleet table's asset classes (and battery units for dual-unit homes).
_ASSET_CLASSES: dict[str, tuple[str, ...]] = {
    "home": ("HOME",),
    "dual_unit": ("HOME",),
    "substation": ("UTILITY_SCALE",),
    "truck": ("MOBILE",),
}
#: An exact rating ("78.4 kWh") matches within this rounding tolerance (ratings are stored as floats).
_EXACT_TOLERANCE = 0.05
_ROW_KEYS = ("hub_id", "bank_id", "zone", "e_kwh", "rated_p_kw", "soc_pct", "health", "asset_class")
_MAX_IDS = 10


def _bounds(low: float | None, high: float | None) -> tuple[float | None, float | None]:
    if low is not None and high is not None and low == high:
        return low - _EXACT_TOLERANCE, high + _EXACT_TOLERANCE
    return low, high


def hub_filter(query: ai_agent.FleetQuery) -> fleet_search.HubFilter:
    """A copilot `FleetQuery` as the Fleet table's own filter: the one filter layer, no second SQL."""
    e_min, e_max = _bounds(query.capacity_min_kwh, query.capacity_max_kwh)
    p_min, p_max = _bounds(query.power_min_kw, query.power_max_kw)
    return fleet_search.HubFilter(
        zones=query.zones,
        bank=query.bank,
        health=tuple(query.health),
        soc_min=query.soc_min_pct,
        soc_max=query.soc_max_pct,
        hw=(query.hw,) if query.hw else (),
        fw=(query.fw,) if query.fw else (),
        # "at home" is a truck-only condition, so it implies the truck class
        asset_class=_ASSET_CLASSES.get(
            query.asset_class or ("truck" if query.at_home is not None else ""), ()
        ),
        e_kwh_min=e_min,
        e_kwh_max=e_max,
        p_kw_min=p_min,
        p_kw_max=p_max,
        units=(2,) if query.asset_class == "dual_unit" else (),
        availability=(query.availability,) if query.availability else (),
    )


async def run_fleet_query(
    rows: fleet_search.FleetRowsStore, th: fleet_search.Thresholds, query: ai_agent.FleetQuery
) -> dict[str, Any]:
    """The copilot's read-only fleet tool: exact counts/totals, the optional breakdown, the first rows
    (lowest charge first when the question is about charge), and for trucks the at-home verdicts. Only
    the fields in `_ROW_KEYS` leave here -- no position, no telemetry series, no customer field."""
    flt = hub_filter(query)
    if query.has_filter and flt.empty:
        # A filter named in the question that did not reach SQL would answer with the whole fleet: refuse
        # (the copilot then says "can't verify") rather than return an unfiltered total.
        raise ValueError("fleet query filter was lost before SQL")
    by_soc = query.soc_min_pct is not None or query.soc_max_pct is not None
    summary = await fleet_search.fleet_summary(
        rows,
        flt,
        th,
        group_by=query.group_by,
        top=ai_agent_fleet.TOP_ROWS,
        sort="soc" if by_soc else "hub",
        descending=query.soc_min_pct is not None and query.soc_max_pct is None,
    )
    result: dict[str, Any] = {
        "total": summary["total"],
        "groups": summary["groups"],
        "group_by": summary["group_by"],
        "rows": [{key: row.get(key) for key in _ROW_KEYS} for row in summary["rows"]],
    }
    if query.asset_class == "truck" or query.at_home is not None:
        units = await fleet_search.mobile_positions_at_home(rows, flt, th)
        home = [u["hub_id"] for u in units if u["at_home"] is True]
        away = [u["hub_id"] for u in units if u["at_home"] is False]
        result["mobile"] = {
            "units": len(units),
            "at_home": len(home),
            "away": len(away),
            "unknown": len(units) - len(home) - len(away),
            "at_home_ids": home[:_MAX_IDS],
            "away_ids": away[:_MAX_IDS],
        }
    return result


def _fleet_tool(
    store: Annotated[StoreProtocol, Depends(get_store)], cfg: Annotated[Config, Depends(get_config)]
) -> ai_agent.FleetTool | None:
    """The fleet tool for this request, or None on a store without the fleet read helper."""
    if not isinstance(store, fleet_search.FleetRowsStore):
        return None
    rows = SelectOnlyRows(store)
    th = fleet_search.Thresholds.from_config(cfg)

    async def tool(query: ai_agent.FleetQuery) -> dict[str, Any]:
        return await run_fleet_query(rows, th, query)

    return tool


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    #: Which console screen the operator is on, for the answer's framing only. Never trusted for access.
    screen: str | None = Field(default=None, max_length=40)


class AskResponse(BaseModel):
    text: str
    tier: str
    citations: list[dict[str, str]]
    model: str | None = None
    provider: str | None = None
    confidence_label: str | None = None
    intent: str | None = None
    refusal_reason: str | None = None
    trace_id: str | None = None
    #: The routing model that screened the question, when one did (the answer text may still be the
    #: console's own; `model` is set only when a model wrote the text).
    screened_by: str | None = None


def _optional_pool(request: Request) -> AsyncConnectionPool | None:
    """Handed only to `GET /og/api/health`'s own handler, whose pool reads are its invariant and
    degraded-mode SELECTs. None in unit tests that never run the app lifespan."""
    return getattr(request.app.state, "pool", None)


async def snapshot(store: StoreProtocol, pool: AsyncConnectionPool | None) -> dict[str, Any]:
    """The read-only view the copilot reasons over: exactly what the console's own GET handlers return
    to a viewer, nothing more.

    A read that fails is named in `unavailable` rather than left as an empty value, so the copilot says
    "can't verify right now" instead of "nothing wrong". The invariant counters count as unavailable
    whenever the health route could not measure them (`invariants_checked_at` is None: its own read
    failed or there is no pool), because the route then reports zeros that were never measured.
    `commitment_switches` is left out: the health route reports it as a constant, not a measurement.
    """
    reader = cast(StoreProtocol, ReadOnlyStore(store))
    unavailable: list[str] = []
    health: dict[str, Any] = {}
    obligations: list[dict[str, Any]] = []
    try:
        health = await health_routes.get_health(store=reader, pool=pool)
    except Exception as exc:
        logger.info("copilot snapshot: health unavailable (%s)", type(exc).__name__)
        unavailable.append("health")
    try:
        rows = await dispatch_routes.list_opportunities(store=reader, _identity=AI_AGENT_IDENTITY, state=None)
        obligations = rows[:_MAX_OBLIGATIONS]
    except Exception as exc:
        logger.info("copilot snapshot: obligations unavailable (%s)", type(exc).__name__)
        unavailable.append("obligations")
    health_view: dict[str, Any] = {}
    if "health" not in unavailable:
        if health.get("invariants_checked_at") is None:
            unavailable.append("invariants")
        else:
            health_view.update(
                {
                    key: health[key]
                    for key in ("reserve_breaches", "double_sold_kwh", "lock_violations")
                    if key in health
                }
            )
        health_view["alerts"] = list(health.get("alerts") or [])[:_MAX_ALERTS]
    view: dict[str, Any] = {
        "health": health_view,
        "hubs": {"counts": dict(health.get("hub_health_counts") or {})},
        "unavailable": unavailable,
    }
    if "obligations" not in unavailable:
        view["obligations"] = obligations
    return view


#: Warning while the copilot's screening fails `[ai_agent].screen_alert_after` (3) times in a row: every
#: answer is then on the no-model tier. Raised and cleared here, through health's single og.alert writer;
#: not health-owned, so health's evaluator never clears it.
SCREENING_ALERT_RULE = "ALR-COPILOT-SCREENING"

#: Whether this process last saw the alert open (None: not yet known since start). The DB is read only
#: when the screening state and this disagree, so a healthy copilot costs no alert query per question.
_screening_alert_open: bool | None = None


async def sync_screening_alert(pool: AsyncConnectionPool | None, health: ScreeningHealth) -> None:
    """Open ALR-COPILOT-SCREENING when screening is failing, clear it on the next success. Never raises:
    the answer has already been given, and alerting must not turn into an error for the operator."""
    global _screening_alert_open
    if pool is None or health.screenings == 0 or _screening_alert_open is health.alerting:
        return
    try:
        open_ids = [a.id for a in await fetch_open_alerts(pool) if a.rule == SCREENING_ALERT_RULE]
        open_now = bool(open_ids)
        if health.alerting and not open_now:
            detail: dict[str, Any] = {
                "consecutive_failures": health.consecutive_failures,
                "last_error": health.last_error,
                "since": health.last_ok_at.isoformat() if health.last_ok_at else None,
            }
            finding = AlertFinding(
                rule=SCREENING_ALERT_RULE,
                severity="warning",
                summary=(
                    f"Copilot screening failed {health.consecutive_failures} times in a row (last: "
                    f"{health.last_error}); answers fall back to console data only"
                ),
                condition_key=SCREENING_ALERT_RULE,
                detail=detail,
            )
            await raise_alert(pool, finding, opened_at=datetime.now(UTC))
        elif not health.alerting and open_now:
            cleared_at = datetime.now(UTC)
            for alert_id in open_ids:
                if alert_id is not None:
                    await clear_alert(pool, alert_id, cleared_at=cleared_at)
        _screening_alert_open = health.alerting
    except Exception as exc:
        logger.warning("copilot screening alert not synced (%s)", type(exc).__name__)


@router.post("/ask", response_model=AskResponse)
async def ask(
    body: AskRequest,
    store: Annotated[StoreProtocol, Depends(get_store)],
    trace: Annotated[TraceStore, Depends(get_trace_store)],
    identity: Annotated[Identity, Depends(require_viewer)],
    pool: Annotated[AsyncConnectionPool | None, Depends(_optional_pool)],
    fleet: Annotated[ai_agent.FleetTool | None, Depends(_fleet_tool)] = None,
) -> AskResponse:
    """Answer one copilot question. Always returns 200: an unavailable or refused assistant is a normal
    answer the panel renders, never an error the console has to handle."""
    context = await snapshot(store, pool)

    async def write_trace(payload: dict[str, Any]) -> str | None:
        ref = await trace.append(f"ai:{identity.user}", "OPERATOR_ACTION", "AI_INTERACTION", payload)
        return str(ref.trace_id)

    answer = await ai_agent.service().ask(
        body.question,
        context,
        trace=write_trace,
        screen=body.screen,
        user=identity.user,
        fleet_tool=fleet,
    )
    await sync_screening_alert(pool, ai_agent.service().screening)
    return AskResponse(
        text=answer.text,
        tier=answer.tier,
        citations=[c.model_dump() for c in answer.citations],
        model=answer.model,
        provider=answer.provider,
        confidence_label=answer.confidence_label,
        intent=answer.intent,
        refusal_reason=answer.refusal_reason,
        trace_id=answer.trace_id,
        screened_by=answer.screened_by,
    )


@router.get("/status")
async def status(_identity: Annotated[Identity, Depends(require_viewer)]) -> dict[str, Any]:
    """What works and how much budget is left (UI-DAT-05, shown on System Health)."""
    return ai_agent.service().status()
