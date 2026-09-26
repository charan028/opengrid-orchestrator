"""The customer caller: an `X-Remote-User` identity (proxy-verified by `opengrid.api.auth`) with role
`customer`, and the customer_id it acts for, from `[api.roles.customer]` (`user = "<customer_id>"`).
Every customer endpoint depends on `require_customer`, and every read or write is scoped to
`CustomerIdentity.customer_id` -- nothing a customer sends can name another customer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, status

from opengrid.api.auth import Identity, Role, current_identity
from opengrid.api.deps import get_config
from opengrid.platform.config import Config

logger = logging.getLogger(__name__)

CUSTOMER_ROLES_KEY = "api.roles.customer"


@dataclass(frozen=True, slots=True)
class CustomerIdentity:
    user: str
    customer_id: UUID


def customer_id_for(user: str, cfg: Config) -> UUID | None:
    """The customer_id `[api.roles.customer]` maps `user` to, or `None` if unmapped or malformed."""
    mapping = cfg.get(CUSTOMER_ROLES_KEY, {}) or {}
    if not isinstance(mapping, dict) or user not in mapping:
        return None
    try:
        return UUID(str(mapping[user]))
    except ValueError:
        logger.error("malformed customer_id mapping", extra={"user": user})
        return None


async def require_customer(
    identity: Annotated[Identity, Depends(current_identity)], cfg: Annotated[Config, Depends(get_config)]
) -> CustomerIdentity:
    """403 unless the caller is a customer with a valid customer_id mapping."""
    if identity.role is not Role.CUSTOMER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="customer role required")
    customer_id = customer_id_for(identity.user, cfg)
    if customer_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="no customer_id mapped for this identity")
    return CustomerIdentity(identity.user, customer_id)
