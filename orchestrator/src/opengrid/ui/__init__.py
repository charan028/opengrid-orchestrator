"""opengrid.ui -- templates, static assets, routes mounted by `opengrid.api` (02b S8: 7 screens +
scenario panel). Owner: ui-a for the base (`templates/base.html`, `templates/_partials/`, `static/`,
`routes/__init__.py`) plus the Control room, Fleet monitoring & control and Health screens; ui-b for
Dispatch, Markets, Profitability and Billing & audit (BUILD.md S4).
"""

from __future__ import annotations

from fastapi import APIRouter

from opengrid.ui.routes import router as _router


def build_router() -> APIRouter:
    """Return the UI's `APIRouter` (Jinja2 templates under `[ui].base_path`, 02b S8), mounted by
    `opengrid.api.create_app()`."""
    return _router
