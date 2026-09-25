from __future__ import annotations

from uuid import uuid4

from opengrid.safestop.pg_backend import PgStopEventBackend

from ._fake_pool import FakePool


async def test_insert_stop_event_sends_expected_params():
    pool = FakePool()
    backend = PgStopEventBackend(pool)  # type: ignore[arg-type]
    stop_id = uuid4()

    await backend.insert_stop_event(
        stop_event_id=stop_id,
        scope_kind="BANK",
        scope_ref="bank-07",
        action="ENGAGE",
        initiator_kind="SAFESTOP_AUTHORITY",
        initiator_ref="operator:alice",
        reason="drill",
        approver_ref=None,
        signature="sig",
    )

    assert len(pool.executed) == 1
    keyword, params = pool.executed[0]
    assert keyword == "INSERT"
    assert params["stop_event_id"] == stop_id
    assert params["scope_kind"] == "BANK"
    assert params["action"] == "ENGAGE"


async def test_latest_action_returns_none_when_no_row():
    pool = FakePool(fetchone_result=None)
    backend = PgStopEventBackend(pool)  # type: ignore[arg-type]
    assert await backend.latest_action("BANK", "bank-07") is None


async def test_latest_action_returns_the_row_value():
    pool = FakePool(fetchone_result=("ENGAGE",))
    backend = PgStopEventBackend(pool)  # type: ignore[arg-type]
    assert await backend.latest_action("BANK", "bank-07") == "ENGAGE"


class _FakeNotify:
    def __init__(self, payload: str) -> None:
        self.payload = payload


class _NotifyConnection:
    def __init__(self, notifications: list[str]) -> None:
        self._notifications = notifications
        self.executed: list[str] = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None

    async def execute(self, sql: str) -> None:
        self.executed.append(sql)

    async def commit(self) -> None:
        self.committed = True

    async def notifies(self):
        for payload in self._notifications:
            yield _FakeNotify(payload)


class _NotifyPool:
    def __init__(self, notifications: list[str]) -> None:
        self._conn = _NotifyConnection(notifications)

    def connection(self):
        return self._conn


async def test_listen_for_requests_yields_parsed_json():
    from opengrid.safestop.pg_backend import REQUEST_CHANNEL, listen_for_requests

    pool = _NotifyPool(['{"action": "PROPOSE", "scope": "BANK"}'])

    received = [payload async for payload in listen_for_requests(pool)]  # type: ignore[arg-type]

    assert received == [{"action": "PROPOSE", "scope": "BANK"}]
    assert pool._conn.executed == [f"LISTEN {REQUEST_CHANNEL}"]
    assert pool._conn.committed


async def test_listen_for_requests_skips_malformed_json():
    from opengrid.safestop.pg_backend import listen_for_requests

    pool = _NotifyPool(["not json", '{"action": "CONFIRM"}'])

    received = [payload async for payload in listen_for_requests(pool)]  # type: ignore[arg-type]

    assert received == [{"action": "CONFIRM"}]
