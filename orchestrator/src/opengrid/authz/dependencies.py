"""`require_action`: the FastAPI dependency factory routers opt into for fine-grained authz
(`Depends(require_action("safestop.engage"))`), separate from `opengrid.authz.enforce` specifically so
`Identity`/`current_identity`/`Role` can be imported here at MODULE scope (real imports, not
`TYPE_CHECKING`-only and not deferred inside a function body).

That matters beyond style: with `from __future__ import annotations` (PEP 563, used throughout this
codebase), every annotation -- including `Annotated[Identity, Depends(current_identity)]` -- is stored as
a string and FastAPI re-evaluates it at route-registration time via `typing.get_type_hints`, which
resolves names against the function's `__globals__` (its *module's* global namespace) -- never an
enclosing function's local namespace, even for a nested closure. A name imported only inside another
function's body, or only under `TYPE_CHECKING`, is invisible to that lookup: FastAPI then can't see the
`Identity`/`Depends(...)` annotation on the `identity` parameter, falls back to treating it as an
ordinary field, and the route answers `422 Unprocessable Entity` on every call instead of running the
dependency at all. (This bit the first version of `require_action`, which defined its inner
`_dependency` closure inside `require_action`'s body with those imports local to `require_action` --
fixed here by moving the whole factory into its own module with top-level imports.)

`opengrid.api.auth` imports `opengrid.authz.enforce` (not this module) at module scope for its own
`policy_engine_for` wiring -- this module importing `opengrid.api.auth` at module scope is therefore a
one-directional edge, not a cycle: `authz.dependencies -> api.auth` and separately `api.auth ->
authz.enforce`, and `authz.enforce` never imports `api.auth`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status

from opengrid.api.auth import Identity, Role, current_identity
from opengrid.api.deps import get_config, get_trace_store
from opengrid.authz.enforce import audit_deny, policy_engine_for
from opengrid.authz.policy import Resource
from opengrid.customer_api.identity import customer_id_for
from opengrid.platform.config import Config
from opengrid.trace.store import TraceStore


def require_action(
    action: str, *, resource_customer_id_param: str | None = None
) -> Callable[..., Awaitable[Identity]]:
    """Dependency factory: `Depends(require_action("safestop.engage"))`. Reads the caller's identity via
    `opengrid.api.auth.current_identity` (so proxy-secret verification always runs first, unchanged),
    decides via the config's `PolicyEngine`, and on deny audits (if the rule asks for it) then raises
    403. On allow, returns the `Identity` unchanged so routers can still read `identity.user`.

    `resource_customer_id_param`: for a customer-scoped action, the name of the path/query parameter
    that carries the resource's own customer_id (as a UUID string) -- callers whose resource has no such
    single parameter should call `opengrid.authz.policy.PolicyEngine.decide` directly instead.
    """

    async def _dependency(
        request: Request,
        identity: Annotated[Identity, Depends(current_identity)],
        cfg: Annotated[Config, Depends(get_config)],
        trace_store: Annotated[TraceStore, Depends(get_trace_store)],
    ) -> Identity:
        engine = policy_engine_for(cfg)
        caller_customer_id = customer_id_for(identity.user, cfg) if identity.role is Role.CUSTOMER else None

        resource: Resource | None = None
        if resource_customer_id_param is not None:
            raw = request.path_params.get(resource_customer_id_param) or request.query_params.get(
                resource_customer_id_param
            )
            resource = Resource(customer_id=UUID(str(raw))) if raw else Resource(customer_id=None)

        decision = engine.decide(
            role=identity.role.value,
            action=action,
            resource=resource,
            caller_customer_id=caller_customer_id,
        )
        if decision.deny:
            await audit_deny(
                trace_store, actor=identity.user, action=action, decision=decision, resource=resource
            )
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail=decision.reason)
        return identity

    return _dependency
