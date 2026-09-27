"""The utility caller (D-33): an `X-Remote-User` identity (proxy-verified by `opengrid.api.auth`) with role
`utility`, bound to the utility_id it acts for by `[api.roles.utility]` (`user = "<utility_id>"`, e.g.
`"og-util-aen" = "AUSTIN_ENERGY"`). Every utility endpoint depends on `require_utility_action`, which
also asks the authz policy (`config/authz.toml`, `utility.*` rules) and audits a denied privileged
action. Nothing the caller sends can name another utility: every read and call is scoped to
`UtilityIdentity.utility_id`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, status

from opengrid.api.auth import Identity, Role, current_identity
from opengrid.api.deps import get_config, get_trace_store
from opengrid.authz.enforce import audit_deny, policy_engine_for
from opengrid.platform.config import Config
from opengrid.trace.store import TraceStore

logger = logging.getLogger(__name__)

UTILITY_ROLES_KEY = "api.roles.utility"
ENABLED_UTILITIES_KEY = "api.utility_api.enabled_utilities"
ACTION_READ = "utility.read"
ACTION_CALL = "utility.call"
ACTION_CANCEL = "utility.cancel"
#: `og.utility.utility_id` values are upper-case identifiers (AUSTIN_ENERGY, CPS_ENERGY).
_UTILITY_ID = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


@dataclass(frozen=True, slots=True)
class UtilityIdentity:
    user: str
    utility_id: str


def utility_id_for(user: str, cfg: Config) -> str | None:
    """The utility_id `[api.roles.utility]` maps `user` to, or None if unmapped or malformed."""
    mapping = cfg.get(UTILITY_ROLES_KEY, {}) or {}
    if not isinstance(mapping, dict) or user not in mapping:
        return None
    utility_id = str(mapping[user])
    if not _UTILITY_ID.match(utility_id):
        logger.error("malformed utility_id mapping", extra={"user": user})
        return None
    return utility_id


def utility_api_enabled_for(cfg: Config) -> frozenset[str]:
    """`[api.utility_api].enabled_utilities`: the utilities that may use the API at all (fail closed:
    none when unset). A mapped identity of a utility with no active contract (e.g. LCRA, RAYBURN, D-37
    sample contracts) stays out until the owner adds it here."""
    return frozenset(str(u) for u in (cfg.get(ENABLED_UTILITIES_KEY, []) or []))


def require_utility_action(action: str) -> Callable[..., Awaitable[UtilityIdentity]]:
    """Dependency factory: 403 unless the caller is a `utility` identity with a valid utility_id mapping
    AND its utility is enabled, AND the authz policy allows `action` for role utility (a denied audited action is traced)."""

    async def _dependency(
        identity: Annotated[Identity, Depends(current_identity)],
        cfg: Annotated[Config, Depends(get_config)],
        trace_store: Annotated[TraceStore, Depends(get_trace_store)],
    ) -> UtilityIdentity:
        decision = policy_engine_for(cfg).decide(role=identity.role.value, action=action)
        if identity.role is not Role.UTILITY or decision.deny:
            await audit_deny(
                trace_store, actor=identity.user, action=action, decision=decision, resource=None
            )
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="utility role required")
        utility_id = utility_id_for(identity.user, cfg)
        if utility_id is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="no utility_id mapped for this identity")
        if utility_id not in utility_api_enabled_for(cfg):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, detail="the utility API is not enabled for this utility"
            )
        return UtilityIdentity(identity.user, utility_id)

    return _dependency
