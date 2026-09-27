"""Shared Jinja2 environment for every screen (`base.html` contract, BUILD.md UI brief). Owner: ui-a.

`NAV_SCREENS` is the fixed left-navigation list every template's `base.html` renders -- the 7 screens at
their fixed paths (BUILD.md UI brief / 02b S8), always relative to `BASE_PATH` (`/og`, matching
`[ui].base_path`'s default, 02b S1.4).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

BASE_PATH = "/og"

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Must match `opengrid.api.csrf.HEADER_NAME` exactly. Not imported directly: `opengrid.api` mounts
# `opengrid.ui` (api -> ui), so importing back from `opengrid.ui` -> `opengrid.api.csrf` would invert
# that dependency direction (BUILD.md S5a) for the sake of one string constant.
CSRF_HEADER_NAME = "X-CSRF-Token"


def _csrf_context(request: Request) -> dict[str, Any]:
    """Exposes the current request's CSRF token (`opengrid.api.csrf.CSRFMiddleware`) to every template
    render without every UI route handler having to pass it explicitly (qa/security-review.md F-03)."""
    return {"csrf_token": getattr(request.state, "csrf_token", ""), "csrf_header_name": CSRF_HEADER_NAME}


# `icon` names a `<symbol id="i-...">` in base.html's inline icon sheet.
NAV_SCREENS: tuple[dict[str, str], ...] = (
    {"label": "Story", "path": f"{BASE_PATH}/story", "icon": "zap"},
    {"label": "Control room", "path": f"{BASE_PATH}/", "icon": "dashboard"},
    {"label": "Fleet", "path": f"{BASE_PATH}/fleet", "icon": "battery"},
    {"label": "Dispatch", "path": f"{BASE_PATH}/dispatch", "icon": "branch"},
    {"label": "Markets", "path": f"{BASE_PATH}/markets", "icon": "trend"},
    {"label": "System Health", "path": f"{BASE_PATH}/health", "icon": "pulse"},
    {"label": "Profitability", "path": f"{BASE_PATH}/profitability", "icon": "dollar"},
    {"label": "Billing & audit", "path": f"{BASE_PATH}/billing", "icon": "scroll"},
    {"label": "Power quality", "path": f"{BASE_PATH}/pq", "icon": "wave"},
)

#: Links out of the console, shown under the screens. `/ogsim/` is the integration-sims control plane
#: (scenarios, anomalies), proxied by Apache with its own sign-in (deploy/apache/opengrid.conf).
SCENARIOS_PATH = "/ogsim/"
NAV_TOOLS: tuple[dict[str, str], ...] = (
    {
        "label": "Scenarios",
        "path": SCENARIOS_PATH,
        "icon": "play",
        "title": "Simulator scenarios and anomalies (opens the sims control plane; separate sign-in)",
    },
)

#: The Help page (r3.4): the last entry of the left navigation on every screen, above the role footer.
HELP_PATH = f"{BASE_PATH}/help"

#: First path segment under `BASE_PATH` -> the Help page section that documents that screen. The Help
#: entry deep-links to it (`/og/help#page-fleet`), so each screen opens the Help page at its own section.
HELP_SECTIONS: dict[str, str] = {
    "": "page-control-room",
    "story": "page-story",
    "fleet": "page-fleet",
    "dispatch": "page-dispatch",
    "markets": "page-markets",
    "health": "page-health",
    "profitability": "page-profitability",
    "billing": "page-billing",
    "pq": "page-pq",
    "help": "page-help",
}


def help_href(path: str) -> str:
    """The Help link for the screen at `path`: `/og/help#page-<screen>`, or the page top for a path that
    is not a documented screen."""
    rest = path[len(BASE_PATH) :] if path.startswith(BASE_PATH) else path
    section = HELP_SECTIONS.get(rest.strip("/").split("/", 1)[0])
    return f"{HELP_PATH}#{section}" if section else HELP_PATH


templates = Jinja2Templates(directory=str(TEMPLATES_DIR), context_processors=[_csrf_context])
templates.env.globals["nav_screens"] = NAV_SCREENS
templates.env.globals["nav_tools"] = NAV_TOOLS
templates.env.globals["base_path"] = BASE_PATH
templates.env.globals["help_path"] = HELP_PATH
templates.env.globals["help_href"] = help_href
