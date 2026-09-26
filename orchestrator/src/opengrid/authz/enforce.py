"""Config-backed loading of `opengrid.authz.policy`'s `PolicyEngine`, plus the trace-store audit write
for a privileged deny. Kept separate from `policy.py` (I/O here, none there) per BUILD.md S5a's "pure
logic separated from I/O" split.

Deliberately has NO import of `opengrid.api.auth` (not even under `TYPE_CHECKING`) -- `opengrid.api.auth`
imports `policy_engine_for` from this module at module scope (its one additive authz wiring line), so
this module importing anything back from `api.auth` would be a circular import. The `require_action`
FastAPI dependency factory, which DOES need `Identity`/`current_identity` from `api.auth`, lives in the
sibling module `opengrid.authz.dependencies` instead -- that one-directional edge
(`authz.dependencies -> api.auth`, and separately `api.auth -> authz.enforce`) has no cycle.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from opengrid.authz.policy import Decision, PolicyEngine, Resource, load_policies_from_file
from opengrid.platform.config import Config
from opengrid.trace.store import TraceStore

_POLICY_PATH_ENV = "OG_AUTHZ_POLICY"
_DEFAULT_POLICY_FILENAME = "authz.toml"


def _policy_path_for(cfg: Config) -> Path:
    """`OG_AUTHZ_POLICY` wins; otherwise the sibling of whatever `orchestrator.toml` was loaded from
    (both configs live in `config/`, BUILD.md's per-workspace layout keeps that pairing); otherwise
    `config/authz.toml` relative to the current working directory (matches how the process is run, per
    `Makefile`/`deploy/`)."""
    override = os.environ.get(_POLICY_PATH_ENV)
    if override:
        return Path(override)
    if cfg.source_path is not None:
        return cfg.source_path.parent / _DEFAULT_POLICY_FILENAME
    return Path("config") / _DEFAULT_POLICY_FILENAME


@lru_cache(maxsize=8)
def _cached_engine(path_str: str) -> PolicyEngine:
    return load_policies_from_file(path_str)


def policy_engine_for(cfg: Config) -> PolicyEngine:
    """The `PolicyEngine` for `cfg`, cached by resolved path (the file is loaded once per process; a
    test that swaps `OG_AUTHZ_POLICY` should call `get_policy_engine.cache_clear()`-equivalent by
    clearing `_cached_engine`, or just pass rules straight to `PolicyEngine` in unit tests instead)."""
    return _cached_engine(str(_policy_path_for(cfg)))


async def audit_deny(
    trace_store: TraceStore, *, actor: str, action: str, decision: Decision, resource: Resource | None
) -> None:
    """K10-style durable record of a privileged deny (authz.toml `audited = true`). Never raises past
    logging concerns of its own; a trace-store failure here still lets the original 403 propagate --
    see `opengrid.authz.dependencies.require_action`, which calls this best-effort before raising."""
    if not decision.audited:
        return
    await trace_store.append(
        stream_id=f"authz_deny:{actor}",
        decision_type="AUTHZ_DENY",
        event_class="TRACE_AUTHZ_DENY",
        payload={
            "actor": actor,
            "action": action,
            "reason": decision.reason,
            "resource_customer_id": str(resource.customer_id) if resource and resource.customer_id else None,
        },
    )
