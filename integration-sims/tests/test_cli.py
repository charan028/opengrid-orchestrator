"""Smoke tests for the ogsim.control CLI (list-types, inject, cancel,
list-active, list-log). Network calls are stubbed by conftest's autouse
fixture; `monkeypatch.chdir` keeps the JSONL log out of the repo.

`inject`/`cancel`/`list-active` default to REST (talking to a running
`ogsim.control serve`); `--local` exercises the in-process fallback path.
REST-mode is tested by monkeypatching `httpx.AsyncClient` (no server needed),
the same pattern as test_market_client.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ogsim.control import cli
from ogsim.control.cli import main


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # conftest's autouse `_isolate_anomaly_log` already points the JSONL log
    # at a per-test tmp_path; this just keeps any other relative-path
    # behaviour (e.g. a future default) confined too.
    monkeypatch.chdir(tmp_path)


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeAsyncClient:
    """Records every call and returns a scripted response, standing in for
    a running control server so REST-mode CLI paths need no real network."""

    calls: list[dict[str, Any]] = []
    response: _FakeResponse = _FakeResponse({"ok": True})

    def __init__(self, *args: Any, **kwargs: Any):
        self.base_url = kwargs.get("base_url")

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def post(self, path: str, json: Any = None) -> _FakeResponse:
        _FakeAsyncClient.calls.append({"method": "POST", "path": path, "json": json})
        return _FakeAsyncClient.response

    async def get(self, path: str) -> _FakeResponse:
        _FakeAsyncClient.calls.append({"method": "GET", "path": path})
        return _FakeAsyncClient.response

    async def delete(self, path: str) -> _FakeResponse:
        _FakeAsyncClient.calls.append({"method": "DELETE", "path": path})
        return _FakeAsyncClient.response


@pytest.fixture
def fake_control_server(monkeypatch: pytest.MonkeyPatch) -> type[_FakeAsyncClient]:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.response = _FakeResponse({"ok": True})
    monkeypatch.setattr(cli.httpx, "AsyncClient", _FakeAsyncClient)
    return _FakeAsyncClient


def test_list_types_prints_the_full_catalogue(capsys: pytest.CaptureFixture[str]):
    assert main(["list-types"]) == 0
    types = json.loads(capsys.readouterr().out)
    assert any(t["id"] == "price_spike" for t in types)


def test_inject_local_prints_the_injected_anomaly(capsys: pytest.CaptureFixture[str]):
    rc = main(["inject", "--local", "--type", "price_spike", "--target", "*", "--duration", "30"])
    assert rc == 0
    record = json.loads(capsys.readouterr().out)
    assert record["type"] == "price_spike"
    assert record["owner"] == "market"


def test_inject_local_unknown_type_fails_with_a_clear_message(capsys: pytest.CaptureFixture[str]):
    rc = main(["inject", "--local", "--type", "not_real", "--target", "*"])
    assert rc == 2
    assert "unknown anomaly type" in capsys.readouterr().err


def test_inject_invalid_params_json_fails_before_any_dispatch(capsys: pytest.CaptureFixture[str]):
    rc = main(["inject", "--local", "--type", "price_spike", "--target", "*", "--params", "{not json"])
    assert rc == 2


def test_inject_rest_posts_to_the_control_server_by_default(
    capsys: pytest.CaptureFixture[str], fake_control_server: type[_FakeAsyncClient]
):
    fake_control_server.response = _FakeResponse({"ok": True, "anomaly": {"id": "x", "type": "price_spike"}})
    rc = main(["inject", "--type", "price_spike", "--target", "*", "--duration", "30"])
    assert rc == 0
    call = fake_control_server.calls[0]
    assert call == {
        "method": "POST",
        "path": "/api/inject",
        "json": {"type": "price_spike", "target": "*", "params": {}, "duration": 30.0, "id": None},
    }


def test_cancel_local_unknown_id_returns_nonzero(capsys: pytest.CaptureFixture[str]):
    rc = main(["cancel", "--local", "--id", "no-such-id"])
    assert rc == 1


def test_cancel_rest_deletes_via_the_control_server(fake_control_server: type[_FakeAsyncClient]):
    fake_control_server.response = _FakeResponse({"ok": True})
    rc = main(["cancel", "--id", "some-id"])
    assert rc == 0
    assert fake_control_server.calls[0] == {"method": "DELETE", "path": "/api/anomalies/some-id"}


def test_list_active_rest_reads_from_the_control_server(fake_control_server: type[_FakeAsyncClient]):
    fake_control_server.response = _FakeResponse({"active": [{"id": "a1"}]})
    rc = main(["list-active"])
    assert rc == 0
    assert fake_control_server.calls[0] == {"method": "GET", "path": "/api/anomalies"}


def test_list_active_local_returns_an_empty_list_with_nothing_injected(capsys: pytest.CaptureFixture[str]):
    assert main(["list-active", "--local"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_control_url_honors_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OGSIM_CONTROL_URL", "http://example.test:9999")
    assert cli._control_url() == "http://example.test:9999"


def test_control_url_defaults_to_localhost_8091(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OGSIM_CONTROL_URL", raising=False)
    assert cli._control_url() == "http://127.0.0.1:8091"


def test_list_log_reflects_a_local_injection_from_a_previous_invocation(capsys: pytest.CaptureFixture[str]):
    # Each `main()` call builds its own in-process Injector, so `list-active
    # --local` (in-memory only) can't see anomalies injected by an earlier
    # process - but the JSONL log is shared on disk and does reflect them.
    main(["inject", "--local", "--type", "hub_offline", "--target", "HUB_1", "--duration", "300"])
    capsys.readouterr()
    assert main(["list-log"]) == 0
    log_lines = capsys.readouterr().out.strip().splitlines()
    assert len(log_lines) == 1
    assert json.loads(log_lines[0])["type"] == "hub_offline"
