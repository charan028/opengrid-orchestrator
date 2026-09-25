"""opengrid.api -- process og-api (02b S7): FastAPI app, auth, REST, SSE. Owner: api agent
(BUILD.md S4).

Trusts `X-Remote-User` from loopback Apache only (`opengrid.api.auth`); binds `127.0.0.1` only
(`[api].bind_host`/`bind_port`, 02b S1.4/S9.3). `/og/api/health` is the one exception: it never
requires `X-Remote-User`, gating instead on the connection itself being loopback, so
`deploy/scripts/deploy.sh`'s direct, unauthenticated poll still works.
"""

from __future__ import annotations

from opengrid.api.app import create_app

__all__ = ["create_app"]
