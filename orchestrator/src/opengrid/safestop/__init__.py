"""opengrid.safestop -- process og-safestop (02a S6.5, 02b S1.2-1.3). Owner: guardian agent
(BUILD.md S4).

Independent stop-only authority (K8): no import of and no runtime dependency on `opengrid.guardian` or
`opengrid.engine`. Talks to the rest of the system only through Postgres (`stop_event`, plus a
LISTEN/NOTIFY request channel, see `pg_backend.py`) and MQTT (retained `<root>/stop/*`).

The public interface below (`Scope`, `engage`, `release`) is fixed by `orchestrator/INTERFACES.md` and
must not change signature without the architect's approval. The actual work is done by
`SafestopService` (`service.py`); these module-level functions delegate to whichever service instance
`configure_service()` last installed, so `main.py` wires the real Postgres/MQTT-backed service at
process startup while tests wire an in-memory fake.
"""

from __future__ import annotations

from typing import Literal

from opengrid.safestop.service import ReleaseNotPermittedError, SafestopService

Scope = Literal["FLEET", "ZONE", "BANK"]

__all__ = ["ReleaseNotPermittedError", "Scope", "configure_service", "engage", "release"]

_service: SafestopService | None = None


class SafestopNotConfiguredError(RuntimeError):
    """`engage()`/`release()` called before `configure_service()` wired a real `SafestopService` --
    only `main.py` (at process startup) and tests should ever hit this."""


def configure_service(service: SafestopService | None) -> None:
    """Install (or, with `None`, clear) the `SafestopService` that `engage()`/`release()` delegate to.
    Called once by `main.py` at startup; tests call it in a fixture to install a fake."""
    global _service
    _service = service


def _require_service() -> SafestopService:
    if _service is None:
        raise SafestopNotConfiguredError("opengrid.safestop.configure_service() was never called")
    return _service


async def engage(scope: Scope, scope_ref: str, reason: str, initiator_ref: str) -> None:
    """Sign (stop-only key) and broadcast a retained ENGAGE stop for `scope`/`scope_ref` (02a S6.5).
    Ramps to zero over the scope's configured window (30s bank / 60s zone / 120s fleet)."""
    await _require_service().engage(scope, scope_ref, reason, initiator_ref)


async def release(scope: Scope, scope_ref: str, approver_ref: str) -> None:
    """RELEASE requires Tier-2 (two-person) approval through the GUARDIAN's signing path -- the
    stop-only key can never call this successfully; per 02a S6.5 this function exists on `safestop`
    only to validate and forward the request, and always fails closed if not co-signed by guardian."""
    await _require_service().release(scope, scope_ref, approver_ref)
