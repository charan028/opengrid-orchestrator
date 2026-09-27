"""opengrid.ui.routes -- aggregates every screen router into the single `router` that
`opengrid.ui.build_router()` returns and `opengrid.api.create_app()` mounts under `[ui].base_path`
(`/og`, 02b S1.4/S8).

ui-a owns this file plus the `control_room`, `fleet` and `health` screens and the shared `static/`
mount (BUILD.md S4). ui-b owns `dispatch.py`, `markets.py`, `profitability.py`, `billing_audit.py` in
this same package -- each is imported defensively so this half of the build never blocks on the other
half landing first.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.staticfiles import StaticFiles

from opengrid.ui.api_client import bind_remote_user
from opengrid.ui.role import remote_user
from opengrid.ui.routes import control_room, copilot, fleet, health, story
from opengrid.ui.routes import help as help_screen
from opengrid.ui.templating import TEMPLATES_DIR

logger = logging.getLogger(__name__)

STATIC_DIR = TEMPLATES_DIR.parent / "static"


async def _forward_remote_user(request: Request) -> None:
    """Bind the request's proxy-verified identity (`opengrid.ui.role.remote_user`: `X-Remote-User` only
    when Apache's `X-OG-Proxy-Auth` secret matched, else `None`) so `opengrid.ui.api_client` forwards it
    to `opengrid.api` on first-paint reads. Never the raw header: the UI must not vouch for an identity
    Apache did not assert. Async on purpose: a sync dependency runs in a worker thread and its
    `ContextVar.set` would not reach the endpoint."""
    bind_remote_user(remote_user(request))


router = APIRouter(dependencies=[Depends(_forward_remote_user)])
router.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
router.include_router(story.router)
router.include_router(control_room.router)
router.include_router(fleet.router)
router.include_router(health.router)
router.include_router(copilot.router)
router.include_router(help_screen.router)

# ui-b's screens (BUILD.md S4 ownership split). Imported by name so a missing module during early build
# degrades to "screen not mounted yet" rather than breaking ui-a's own screens or the app startup.
_UI_B_SCREENS = ("dispatch", "markets", "profitability", "billing_audit", "pq", "alerts")
for _screen in _UI_B_SCREENS:
    try:
        _module = __import__(f"opengrid.ui.routes.{_screen}", fromlist=["router"])
    except ImportError:
        logger.warning("ui-b screen module opengrid.ui.routes.%s is not present yet; skipping", _screen)
        continue
    router.include_router(_module.router)

__all__ = ["router"]
