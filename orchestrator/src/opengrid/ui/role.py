"""Operator/viewer role resolution and identity forwarding for `opengrid.ui` screens and route guards.

Security fix (live-path finding, 2026-09-26): this module used to trust a client-supplied `X-OG-Role`
header directly ("Apache is trusted to only ever forward a known role"). That trust was misplaced --
Apache's actual reverse-proxy fragment for `/og/` (`/etc/apache2/conf-enabled/opengrid.conf` on the base
server, confirmed by reading the live config) only ever touches `X-Remote-User`
(`RequestHeader unset X-Remote-User` before `RequestHeader set X-Remote-User expr=%{REMOTE_USER}`); it
never sets, unsets, or otherwise looks at `X-OG-Role`. That header reached this process completely
unmolested, so any caller of `https://base.tocy-net.net/og/...` could add `X-OG-Role: operator` to their
own request and have every UI-level operator gate built on `is_operator()` (fleet manual command, scoped
safe-stop engage, alert ack) believe them, real role notwithstanding.

Identity now comes ONLY from `X-Remote-User` -- the header Apache itself sets from `REMOTE_USER` after
stripping any client-supplied value (`opengrid.api.auth`'s docstring and threat model, which already got
this right for the REST layer). Since `opengrid.ui` is mounted inside the very same FastAPI app as
`opengrid.api` (`opengrid.api.app._mount_ui`), the incoming UI request carries that same
Apache-authenticated header, and the role mapping is the one already-owned `[api.roles]` table
(`opengrid.api.auth.role_for_identity`) -- reused here, not duplicated, so there is exactly one place
that maps an identity to a role.

A missing or unmapped identity denies: `role_of` returns `"viewer"` (read-only at most), matching
`opengrid.api.auth.current_identity`'s deny-by-default posture -- it never defaults an *unrecognised*
identity to `"operator"`. `is_operator`/`role_of` gate the UI's own routes server-side (not just template
visibility), but they are not the authoritative check: every write this module relays forwards the same
`remote_user` to the API (`opengrid.ui.api_client.post_json(..., remote_user=remote_user(request))`),
which re-checks it independently against `opengrid.api.auth.require_operator` (defence in depth -- the
UI gate is a usability nicety, not the security boundary).

Owner: ui-a (BUILD.md S4).
"""

from __future__ import annotations

import logging

from fastapi import Request

from opengrid.api.auth import Role, role_for_identity, verified_remote_user
from opengrid.api.deps import get_config

logger = logging.getLogger(__name__)

VIEWER = Role.VIEWER.value
OPERATOR = Role.OPERATOR.value


def remote_user(request: Request) -> str | None:
    """The Apache-authenticated identity for this request (`X-Remote-User`), or `None` if absent or not
    asserted by Apache (`opengrid.api.auth.verified_remote_user`: the proxy secret must match, since any
    local process can reach the loopback port). Shared by every UI route so
    `opengrid.ui.api_client.post_json`'s `remote_user=` always forwards the same value
    `role_of`/`is_operator` used to decide access here -- never a second, independent read of the
    header."""
    return verified_remote_user(request)


def role_of(request: Request) -> str:
    """`"operator"` or `"viewer"` (default, deny-by-default) for the current request, resolved from
    the trusted `X-Remote-User` identity through `[api.roles]` config -- never from a client-supplied
    header. Missing config (e.g. a bare test harness with no `app.state.config`) is treated the same
    as an unmapped identity: logged and denied down to `"viewer"`, never escalated."""
    user = remote_user(request)
    if user is None:
        return VIEWER
    try:
        cfg = get_config(request)
    except AttributeError:
        logger.warning("role_of: app.state.config is not set; denying identity %r to viewer", user)
        return VIEWER
    role = role_for_identity(user, cfg)
    if role is None:
        logger.warning("no role mapped for identity %r; defaulting to %s", user, VIEWER)
        return VIEWER
    return role.value


def is_operator(request: Request) -> bool:
    """True only for an operator; templates use this to hide write actions, and route guards use it to
    deny the request server-side (BUILD.md UI brief). Always paired with forwarding `remote_user(request)`
    to the API on the write itself -- this function alone is not the security boundary (see module
    docstring)."""
    return role_of(request) == OPERATOR
