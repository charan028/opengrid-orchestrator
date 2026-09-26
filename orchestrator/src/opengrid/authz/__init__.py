"""Authorization: rules-as-data policy engine (role x action x resource-scope) plus an OIDC-ready
identity abstraction, owner-approved 2026-09-26 as the "light version" of identity/HA (no Keycloak).

`opengrid.authz.policy` is pure (no I/O, no FastAPI): `decide(identity, action, resource)` matches
`authz.toml`-loaded rules and returns an `Allow`/`Deny` `Decision`, deny by default. `opengrid.authz.enforce`
loads the config-backed `PolicyEngine` and does the trace-store audit write for privileged denies (it
never imports `opengrid.api.auth`, so that `api.auth -> authz.enforce` import stays one-directional).
`opengrid.authz.dependencies` adds the FastAPI dependency (`require_action`) that DOES need
`opengrid.api.auth`'s `Identity`/`current_identity` -- kept in its own module for exactly that reason.
`opengrid.authz.identity` adds the `IdentityProvider` protocol: today's Apache proxy-secret model
(`opengrid.api.auth`) is untouched and remains the default; `OidcIdentity` is an alternative, OFF by
default, config-selected.

This package intentionally does NOT import `opengrid.authz.dependencies` (or anything else that imports
`opengrid.api.auth`) from `__init__.py` -- `opengrid.api.auth` imports `opengrid.authz.enforce` at module
scope, and importing a submodule always fully initializes this package's `__init__.py` first, so any
`api.auth` import from here would risk resolving names on a partially-initialized `opengrid.api.auth`
module. Import `opengrid.authz.dependencies` directly (`from opengrid.authz.dependencies import
require_action`) instead of via this package's namespace.
"""

from opengrid.authz.policy import (
    Decision,
    PolicyEngine,
    Resource,
    Rule,
    load_policies,
)

__all__ = [
    "Decision",
    "PolicyEngine",
    "Resource",
    "Rule",
    "load_policies",
]
