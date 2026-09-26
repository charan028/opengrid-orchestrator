"""ERCOT_AS deployment intake from the market simulator (build phase 2026-09-26).

The market sim declares simulated AS deployments at `GET {base_url}/admin/as_deployment`:
`{"active": null}` when none is declared, else `{"active": {"id", "service", "deployed_mw", "recall",
"declared_at"}}`. This poller mirrors that into `og.as_deployment` (migration 0020) exactly like an
operator-declared deployment (`POST /og/api/dispatch/as-deployments`), with `source = 'MARKET_SIM'`
(allowed by 0020's CHECK) -- the allocator/invariants read the table and never know the difference.

Semantics (one `poll_once` per `interval_s`):
- active, not recalled: open a row for that sim id (`requested_by = "market_sim:<id>"`) if none is open,
  else extend it. The sim publishes no end time, so every row is a LEASE: `end_at = now + lease_s`,
  renewed on each poll. If the sim or this poller goes quiet the deployment ends on its own (fail-safe:
  a held award never keeps discharging on a stale declaration).
- `recall: true`: end that sim id's open row now (`cancelled_at`, never a delete -- audit trail).
- `active: null`, or a different sim id: end every other open MARKET_SIM row now.
- HTTP failure or an unreadable payload: no change (the lease handles it) and a warning.

`obligation_id` is left NULL -- "deploys every ERCOT_AS award", as the operator endpoint's default; the
sim's `service` (RRS/ECRS/...) is recorded in `reason`. Every open/end is traced first (K10) as
`FEED_CHANGE` on stream `market_sim` when a `TraceStore` is given.

Off by default: `[market_sim].as_deployment_poll = false`. `build_as_deployment_poller` returns `None`
unless it is switched on.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

import httpx
from psycopg_pool import AsyncConnectionPool

from opengrid.trace.store import TraceStore

logger = logging.getLogger(__name__)

SOURCE_MARKET_SIM = "MARKET_SIM"
TRACE_STREAM = "market_sim"
REQUESTED_BY_PREFIX = "market_sim:"

DEFAULT_BASE_URL = "http://127.0.0.1:8090"  # ogsim.market's loopback default (OGSIM_MARKET_PORT)
DEFAULT_INTERVAL_S = 10.0
DEFAULT_LEASE_S = 60.0
DEFAULT_TIMEOUT_S = 5.0


@dataclass(frozen=True, slots=True)
class PollConfig:
    enabled: bool
    base_url: str
    interval_s: float
    lease_s: float


def poll_config_from(cfg: object) -> PollConfig:
    """`[market_sim]`: `as_deployment_poll` (default false), `base_url`, `as_deployment_poll_interval_s`,
    `as_deployment_lease_s`. `cfg` is duck-typed (`.get(dotted_key, default)`, i.e. `Config`)."""

    def get(key: str, default: Any) -> Any:
        return cfg.get(f"market_sim.{key}", default) if hasattr(cfg, "get") else default

    interval_s = float(get("as_deployment_poll_interval_s", DEFAULT_INTERVAL_S))
    lease_s = float(get("as_deployment_lease_s", DEFAULT_LEASE_S))
    if lease_s <= interval_s:
        raise ValueError("[market_sim].as_deployment_lease_s must exceed as_deployment_poll_interval_s")
    return PollConfig(
        enabled=bool(get("as_deployment_poll", False)),
        base_url=str(get("base_url", DEFAULT_BASE_URL)).rstrip("/"),
        interval_s=interval_s,
        lease_s=lease_s,
    )


@dataclass(frozen=True, slots=True)
class SimDeployment:
    """The sim's `active` object, validated."""

    sim_id: str
    service: str
    deployed_mw: float
    recall: bool
    declared_at: datetime

    @property
    def requested_by(self) -> str:
        return f"{REQUESTED_BY_PREFIX}{self.sim_id}"


class PayloadError(ValueError):
    """The sim's response did not have the documented shape."""


def parse_active(payload: object) -> SimDeployment | None:
    """`{"active": null}` -> None; `{"active": {...}}` -> `SimDeployment`; anything else -> `PayloadError`."""
    if not isinstance(payload, Mapping) or "active" not in payload:
        raise PayloadError("missing 'active'")
    active = payload["active"]
    if active is None:
        return None
    if not isinstance(active, Mapping):
        raise PayloadError("'active' is not an object")
    try:
        declared_at = datetime.fromisoformat(str(active["declared_at"]))
        return SimDeployment(
            sim_id=str(active["id"]),
            service=str(active.get("service", "")),
            deployed_mw=float(active.get("deployed_mw", 0.0)),
            recall=bool(active.get("recall", False)),
            declared_at=declared_at if declared_at.tzinfo else declared_at.replace(tzinfo=UTC),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PayloadError(f"malformed 'active': {exc}") from exc


@dataclass(frozen=True, slots=True)
class OpenDeployment:
    """An open (not cancelled, lease not expired) MARKET_SIM row."""

    deployment_id: UUID
    requested_by: str


class AsDeploymentRepo(Protocol):
    async def list_open(self, *, now: datetime) -> list[OpenDeployment]: ...

    async def insert(
        self, *, deployment_id: UUID, start_at: datetime, end_at: datetime, requested_by: str, reason: str
    ) -> None: ...

    async def extend(self, deployment_id: UUID, *, end_at: datetime) -> None: ...

    async def close(self, deployment_id: UUID, *, at: datetime) -> None: ...


_LIST_OPEN_SQL = """
    SELECT deployment_id, requested_by FROM og.as_deployment
    WHERE source = 'MARKET_SIM' AND cancelled_at IS NULL AND end_at > %(now)s
    ORDER BY start_at
"""

_INSERT_SQL = """
    INSERT INTO og.as_deployment (deployment_id, obligation_id, start_at, end_at, source, requested_by, reason)
    VALUES (%(deployment_id)s, NULL, %(start_at)s, %(end_at)s, 'MARKET_SIM', %(requested_by)s, %(reason)s)
"""

_EXTEND_SQL = """
    UPDATE og.as_deployment SET end_at = %(end_at)s
    WHERE deployment_id = %(deployment_id)s AND source = 'MARKET_SIM' AND cancelled_at IS NULL
"""

_CLOSE_SQL = """
    UPDATE og.as_deployment SET cancelled_at = %(at)s
    WHERE deployment_id = %(deployment_id)s AND source = 'MARKET_SIM' AND cancelled_at IS NULL
"""


class PgAsDeploymentRepo:
    """`AsDeploymentRepo` over `og.as_deployment`; touches only `source = 'MARKET_SIM'` rows, so an
    operator's own deployments are never extended or ended by the poller."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def list_open(self, *, now: datetime) -> list[OpenDeployment]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_LIST_OPEN_SQL, {"now": now})
            rows = await cur.fetchall()
        return [OpenDeployment(deployment_id=r[0], requested_by=str(r[1] or "")) for r in rows]

    async def _write(self, sql: str, params: dict[str, Any]) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
            await conn.commit()

    async def insert(
        self, *, deployment_id: UUID, start_at: datetime, end_at: datetime, requested_by: str, reason: str
    ) -> None:
        await self._write(
            _INSERT_SQL,
            {
                "deployment_id": deployment_id,
                "start_at": start_at,
                "end_at": end_at,
                "requested_by": requested_by,
                "reason": reason,
            },
        )

    async def extend(self, deployment_id: UUID, *, end_at: datetime) -> None:
        await self._write(_EXTEND_SQL, {"deployment_id": deployment_id, "end_at": end_at})

    async def close(self, deployment_id: UUID, *, at: datetime) -> None:
        await self._write(_CLOSE_SQL, {"deployment_id": deployment_id, "at": at})


@dataclass
class AsDeploymentPoller:
    http_client: httpx.AsyncClient
    repo: AsDeploymentRepo
    config: PollConfig
    trace: TraceStore | None = None
    _next_poll_at: datetime | None = field(default=None, init=False)

    @property
    def url(self) -> str:
        return f"{self.config.base_url}/admin/as_deployment"

    async def poll_if_due(self, *, now: datetime | None = None) -> None:
        """For a host loop ticking faster than `interval_s` (og-feeds' 5 s tick)."""
        now = now or datetime.now(UTC)
        if self._next_poll_at is not None and now < self._next_poll_at:
            return
        self._next_poll_at = now + timedelta(seconds=self.config.interval_s)
        await self.poll_once(now=now)

    async def poll_once(self, *, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        try:
            response = await self.http_client.get(self.url, timeout=DEFAULT_TIMEOUT_S)
            response.raise_for_status()
            active = parse_active(response.json())
        except (httpx.HTTPError, ValueError) as exc:  # PayloadError and JSON decode errors are ValueErrors
            logger.warning(
                "market sim AS deployment poll failed; leases left to expire", extra={"error": str(exc)}
            )
            return
        await self.apply(active, now=now)

    async def apply(self, active: SimDeployment | None, *, now: datetime) -> None:
        """Reconcile the open MARKET_SIM rows with the sim's current declaration (module docstring)."""
        open_rows = await self.repo.list_open(now=now)
        keep = active.requested_by if active is not None and not active.recall else None
        for row in open_rows:
            if row.requested_by != keep:
                await self._trace(
                    "AS_DEPLOYMENT_END", {"deployment_id": str(row.deployment_id), "sim": row.requested_by}
                )
                await self.repo.close(row.deployment_id, at=now)
        if keep is None or active is None:
            return
        end_at = now + timedelta(seconds=self.config.lease_s)
        current = [row for row in open_rows if row.requested_by == keep]
        if current:
            for row in current:
                await self.repo.extend(row.deployment_id, end_at=end_at)
            return
        deployment_id = uuid4()
        start_at = min(active.declared_at, now)
        reason = f"market sim {active.service} deployment, {active.deployed_mw:g} MW"
        await self._trace(
            "AS_DEPLOYMENT_START",
            {
                "deployment_id": str(deployment_id),
                "sim": keep,
                "service": active.service,
                "deployed_mw": active.deployed_mw,
                "start_at": start_at.isoformat(),
                "end_at": end_at.isoformat(),
            },
        )
        await self.repo.insert(
            deployment_id=deployment_id, start_at=start_at, end_at=end_at, requested_by=keep, reason=reason
        )

    async def _trace(self, event_class: str, payload: dict[str, Any]) -> None:
        if self.trace is not None:
            await self.trace.append(TRACE_STREAM, "FEED_CHANGE", event_class, payload)


def build_as_deployment_poller(
    cfg: object,
    pool: AsyncConnectionPool,
    http_client: httpx.AsyncClient,
    *,
    trace: TraceStore | None = None,
) -> AsDeploymentPoller | None:
    """The poller for a host process, or `None` while `[market_sim].as_deployment_poll` is false."""
    config = poll_config_from(cfg)
    if not config.enabled:
        return None
    return AsDeploymentPoller(
        http_client=http_client, repo=PgAsDeploymentRepo(pool), config=config, trace=trace
    )
