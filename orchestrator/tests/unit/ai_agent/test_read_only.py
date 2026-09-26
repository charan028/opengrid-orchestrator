"""The copilot is read-only by construction (issue #26 review item 4).

* `opengrid.ai_agent` imports no write or command module -- not the fleet command path, safe stop,
  dispatch/allocator writes, contract lifecycle writes, the guardian, the trace writer, the API, or a
  database / MQTT driver. Checked two ways: statically over every module's imports, and at runtime by
  importing the package in a fresh interpreter and inspecting `sys.modules`.
* The API router builds the snapshot through the console's own GET handlers as the `og-ai-agent` viewer
  identity, over a store view that exposes only read methods.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import opengrid.ai_agent as ai_agent_pkg
from opengrid import ai_agent
from opengrid.ai_agent import CopilotService
from opengrid.ai_agent.budgets import Budget, BudgetLimits
from opengrid.ai_agent.gateway import ModelGateway
from opengrid.api.auth import Identity, Role
from opengrid.api.routers import ai as ai_routes

from ..api.fakes import FakeStore
from .fakes import FakeProvider

#: Module prefixes the copilot must never import: anything that writes, commands, signs, or talks to
#: the database or the broker.
FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "opengrid.api",
    "opengrid.fleet",
    "opengrid.safestop",
    "opengrid.guardian",
    "opengrid.engine",
    "opengrid.allocator",
    "opengrid.selector",
    "opengrid.contracts",
    "opengrid.settle",
    "opengrid.trace",
    "opengrid.ledger",
    "opengrid.platform.db",
    "opengrid.customer_api",
    "psycopg",
    "psycopg_pool",
    "aiomqtt",
    "paho",
)


def _forbidden(module: str) -> bool:
    return any(module == prefix or module.startswith(prefix + ".") for prefix in FORBIDDEN_PREFIXES)


def test_no_ai_agent_module_imports_a_write_or_command_module() -> None:
    package_dir = Path(ai_agent_pkg.__file__).parent
    offenders: list[str] = []
    for source in sorted(package_dir.rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
            offenders += [f"{source.name}: {name}" for name in names if _forbidden(name)]
    assert offenders == []


def test_importing_the_copilot_loads_no_write_or_command_module() -> None:
    """The runtime check catches what the static one cannot: a transitive import through a helper."""
    probe = (
        "import json, sys\n"
        "import opengrid.ai_agent, opengrid.ai_agent.claude, opengrid.ai_agent.gateway\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    result = subprocess.run(  # noqa: S603 -- fixed argv, this interpreter
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=60
    )
    loaded = json.loads(result.stdout)
    assert [name for name in loaded if _forbidden(name)] == []


# --- the router's snapshot ------------------------------------------------------------------------


class _RecordingStore:
    """Wraps the API fake store and records every attribute the snapshot reaches for."""

    def __init__(self) -> None:
        self._inner = FakeStore()
        self.touched: list[str] = []

    def __getattr__(self, name: str) -> Any:
        self.touched.append(name)
        return getattr(self._inner, name)


def test_the_store_view_refuses_every_write() -> None:
    view = ai_routes.ReadOnlyStore(FakeStore())  # type: ignore[arg-type]

    for write in ("ack_alert", "insert_command_batch", "record_operator_action", "create_opportunity"):
        with pytest.raises(AttributeError):
            getattr(view, write)
    assert callable(view.list_obligations)


async def test_the_snapshot_is_built_from_the_viewer_get_handlers_only() -> None:
    store = _RecordingStore()

    snapshot = await ai_routes.snapshot(store, None)  # type: ignore[arg-type]

    assert set(store.touched) <= ai_routes.READ_METHODS
    assert snapshot["obligations"], "obligations come from GET /og/api/dispatch/opportunities"
    assert snapshot["health"]["alerts"][0]["summary"] == "ERCOT price feed stale"
    assert "hubs" in snapshot and "counts" in snapshot["hubs"]


def test_the_service_identity_is_a_viewer() -> None:
    assert ai_routes.AI_AGENT_IDENTITY.role is Role.VIEWER
    assert ai_routes.AI_AGENT_IDENTITY.user == "og-ai-agent"


# --- the router's trace rule (review item 6, end to end) -----------------------------------------


class _TraceRef:
    trace_id = "00000000-0000-0000-0000-00000000a1a1"


class _TraceStore:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.appended: list[tuple[str, str, str, dict[str, Any]]] = []

    async def append(
        self, stream_id: str, decision_type: str, event_class: str, payload: dict[str, Any]
    ) -> Any:
        if self.fail:
            raise ConnectionError("trace backend down")
        self.appended.append((stream_id, decision_type, event_class, payload))
        return _TraceRef()


@pytest.fixture
def _copilot() -> Any:
    service = CopilotService(
        gateway=ModelGateway(primary=FakeProvider(intent="explain_decision"), budget=Budget(BudgetLimits()))
    )
    ai_agent.set_service(service)
    yield service
    ai_agent.set_service(None)


async def test_the_route_traces_to_the_ai_stream(_copilot: Any) -> None:
    trace = _TraceStore()

    response = await ai_routes.ask(
        ai_routes.AskRequest(question="explain the guardian's reasoning"),
        FakeStore(),  # type: ignore[arg-type]
        trace,  # type: ignore[arg-type]
        Identity("viewer", Role.VIEWER),
        None,
    )

    assert response.tier == "prose" and response.provider == "claude"
    assert response.trace_id == _TraceRef.trace_id
    stream, decision_type, event_class, payload = trace.appended[0]
    assert (stream, decision_type, event_class) == ("ai:viewer", "OPERATOR_ACTION", "AI_INTERACTION")
    assert payload["provider"] == "claude" and payload["payload_sha256"]


async def test_the_route_withholds_an_untraced_answer(_copilot: Any) -> None:
    response = await ai_routes.ask(
        ai_routes.AskRequest(question="explain the guardian's reasoning"),
        FakeStore(),  # type: ignore[arg-type]
        _TraceStore(fail=True),  # type: ignore[arg-type]
        Identity("viewer", Role.VIEWER),
        None,
    )

    assert response.tier == "unavailable"
    assert "assistant unavailable" in response.text
    assert response.model is None and response.trace_id is None
