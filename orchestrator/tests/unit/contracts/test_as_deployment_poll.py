"""Market-sim AS deployment intake (`opengrid.contracts.as_deployment_poll`) against a fake HTTP transport
and an in-memory `og.as_deployment`: open on declare, lease renewal, close on recall / on clear, fail-safe
on sim errors, off by default."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest

from opengrid.contracts.as_deployment_poll import (
    AsDeploymentPoller,
    OpenDeployment,
    PayloadError,
    PgAsDeploymentRepo,
    PollConfig,
    build_as_deployment_poller,
    parse_active,
    poll_config_from,
)

T0 = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
CONFIG = PollConfig(enabled=True, base_url="http://sim", interval_s=10.0, lease_s=60.0)


def _active(sim_id: str = "dep-1", *, recall: bool = False, declared_at: datetime = T0) -> dict[str, Any]:
    return {
        "active": {
            "id": sim_id,
            "service": "RRS",
            "deployed_mw": 50.0,
            "recall": recall,
            "declared_at": declared_at.isoformat(),
        }
    }


@dataclass
class _Row:
    deployment_id: UUID
    start_at: datetime
    end_at: datetime
    requested_by: str
    reason: str
    cancelled_at: datetime | None = None


@dataclass
class _Repo:
    rows: list[_Row] = field(default_factory=list)

    async def list_open(self, *, now: datetime) -> list[OpenDeployment]:
        return [
            OpenDeployment(r.deployment_id, r.requested_by)
            for r in self.rows
            if r.cancelled_at is None and r.end_at > now
        ]

    async def insert(self, *, deployment_id, start_at, end_at, requested_by, reason) -> None:  # type: ignore[no-untyped-def]
        assert end_at > start_at  # og.as_deployment's as_deployment_window CHECK
        self.rows.append(_Row(deployment_id, start_at, end_at, requested_by, reason))

    async def extend(self, deployment_id: UUID, *, end_at: datetime) -> None:
        next(r for r in self.rows if r.deployment_id == deployment_id).end_at = end_at

    async def close(self, deployment_id: UUID, *, at: datetime) -> None:
        next(r for r in self.rows if r.deployment_id == deployment_id).cancelled_at = at


@dataclass
class _Trace:
    events: list[tuple[str, str, str, dict[str, Any]]] = field(default_factory=list)

    async def append(
        self, stream_id: str, decision_type: str, event_class: str, payload: dict[str, Any]
    ) -> None:
        self.events.append((stream_id, decision_type, event_class, payload))


class _Sim:
    """A fake market sim: `payload` (or `status`) is what the next GET returns."""

    def __init__(self) -> None:
        self.payload: Any = {"active": None}
        self.status = 200
        self.paths: list[str] = []

    def client(self) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            self.paths.append(request.url.path)
            if self.status != 200:
                return httpx.Response(self.status)
            return httpx.Response(200, json=self.payload)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _poller(sim: _Sim, repo: _Repo, trace: _Trace | None = None) -> AsDeploymentPoller:
    return AsDeploymentPoller(http_client=sim.client(), repo=repo, config=CONFIG, trace=trace)  # type: ignore[arg-type]


async def test_declared_deployment_opens_a_market_sim_row_with_a_lease() -> None:
    sim, repo, trace = _Sim(), _Repo(), _Trace()
    sim.payload = _active(declared_at=T0 - timedelta(seconds=5))
    await _poller(sim, repo, trace).poll_once(now=T0)

    assert sim.paths == ["/admin/as_deployment"]
    [row] = repo.rows
    assert row.requested_by == "market_sim:dep-1"
    assert row.start_at == T0 - timedelta(seconds=5)
    assert row.end_at == T0 + timedelta(seconds=60)
    assert "RRS" in row.reason
    assert [(e[0], e[1], e[2]) for e in trace.events] == [
        ("market_sim", "FEED_CHANGE", "AS_DEPLOYMENT_START")
    ]


async def test_still_active_deployment_renews_its_lease_without_a_second_row() -> None:
    sim, repo = _Sim(), _Repo()
    sim.payload = _active()
    poller = _poller(sim, repo)
    await poller.poll_once(now=T0)
    await poller.poll_once(now=T0 + timedelta(seconds=30))

    [row] = repo.rows
    assert row.end_at == T0 + timedelta(seconds=90)
    assert row.cancelled_at is None


async def test_recall_closes_the_deployment_and_never_reopens_it() -> None:
    sim, repo, trace = _Sim(), _Repo(), _Trace()
    sim.payload = _active()
    poller = _poller(sim, repo, trace)
    await poller.poll_once(now=T0)
    sim.payload = _active(recall=True)
    await poller.poll_once(now=T0 + timedelta(seconds=10))
    await poller.poll_once(now=T0 + timedelta(seconds=20))  # the sim keeps reporting the recalled anomaly

    [row] = repo.rows
    assert row.cancelled_at == T0 + timedelta(seconds=10)
    assert [e[2] for e in trace.events] == ["AS_DEPLOYMENT_START", "AS_DEPLOYMENT_END"]


async def test_cleared_or_replaced_declaration_closes_the_old_row() -> None:
    sim, repo = _Sim(), _Repo()
    poller = _poller(sim, repo)
    sim.payload = _active("dep-1")
    await poller.poll_once(now=T0)
    sim.payload = _active("dep-2")
    await poller.poll_once(now=T0 + timedelta(seconds=10))
    assert [(r.requested_by, r.cancelled_at is None) for r in repo.rows] == [
        ("market_sim:dep-1", False),
        ("market_sim:dep-2", True),
    ]
    sim.payload = {"active": None}
    await poller.poll_once(now=T0 + timedelta(seconds=20))
    assert all(r.cancelled_at is not None for r in repo.rows)


async def test_a_recall_seen_first_opens_nothing() -> None:
    sim, repo = _Sim(), _Repo()
    sim.payload = _active(recall=True)
    await _poller(sim, repo).poll_once(now=T0)
    assert repo.rows == []


@pytest.mark.parametrize("failure", ["http_500", "bad_payload"])
async def test_sim_failure_changes_nothing_so_the_lease_expires(failure: str) -> None:
    sim, repo = _Sim(), _Repo()
    sim.payload = _active()
    poller = _poller(sim, repo)
    await poller.poll_once(now=T0)
    if failure == "http_500":
        sim.status = 500
    else:
        sim.payload = {"unexpected": True}
    await poller.poll_once(now=T0 + timedelta(seconds=10))

    [row] = repo.rows
    assert row.cancelled_at is None
    assert row.end_at == T0 + timedelta(seconds=60)  # not renewed: ends on its own at the lease


async def test_poll_if_due_throttles_to_the_interval() -> None:
    sim, repo = _Sim(), _Repo()
    poller = _poller(sim, repo)
    await poller.poll_if_due(now=T0)
    await poller.poll_if_due(now=T0 + timedelta(seconds=5))
    await poller.poll_if_due(now=T0 + timedelta(seconds=10))
    assert len(sim.paths) == 2


def test_parse_active_shapes() -> None:
    assert parse_active({"active": None}) is None
    parsed = parse_active(_active())
    assert parsed is not None and parsed.sim_id == "dep-1" and parsed.declared_at == T0
    for bad in ({}, {"active": 3}, {"active": {"service": "RRS"}}, []):
        with pytest.raises(PayloadError):
            parse_active(bad)


class _Cfg:
    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)


def test_off_by_default_and_config_keys() -> None:
    assert poll_config_from(_Cfg({})).enabled is False
    assert build_as_deployment_poller(_Cfg({}), pool=None, http_client=None) is None  # type: ignore[arg-type]
    cfg = poll_config_from(
        _Cfg(
            {
                "market_sim.as_deployment_poll": True,
                "market_sim.base_url": "http://127.0.0.1:8090/",
                "market_sim.as_deployment_poll_interval_s": 5,
                "market_sim.as_deployment_lease_s": 30,
            }
        )
    )
    assert cfg == PollConfig(enabled=True, base_url="http://127.0.0.1:8090", interval_s=5.0, lease_s=30.0)
    with pytest.raises(ValueError, match="lease"):
        poll_config_from(_Cfg({"market_sim.as_deployment_lease_s": 5}))


# --- PgAsDeploymentRepo SQL shape (fake pool) ------------------------------------------------------------


class _Cursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, sql: str, params: dict[str, Any]) -> None:
        self.executed.append((sql, params))

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return [(UUID(int=1), "market_sim:dep-1")]

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Conn:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor
        self.commits = 0

    def cursor(self) -> _Cursor:
        return self._cursor

    async def commit(self) -> None:
        self.commits += 1

    async def __aenter__(self) -> _Conn:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Pool:
    def __init__(self) -> None:
        self.cursor = _Cursor()
        self.conn = _Conn(self.cursor)

    def connection(self) -> _Conn:
        return self.conn


async def test_pg_repo_only_touches_market_sim_rows() -> None:
    pool = _Pool()
    repo = PgAsDeploymentRepo(pool)  # type: ignore[arg-type]
    assert await repo.list_open(now=T0) == [OpenDeployment(UUID(int=1), "market_sim:dep-1")]
    await repo.insert(
        deployment_id=UUID(int=2),
        start_at=T0,
        end_at=T0 + timedelta(minutes=1),
        requested_by="market_sim:x",
        reason="r",
    )
    await repo.extend(UUID(int=2), end_at=T0 + timedelta(minutes=2))
    await repo.close(UUID(int=2), at=T0)
    sqls = [sql for sql, _ in pool.cursor.executed]
    assert all("MARKET_SIM" in sql for sql in sqls)
    assert "INSERT INTO og.as_deployment" in sqls[1]
    assert "cancelled_at = %(at)s" in sqls[3]
    assert pool.conn.commits == 3
