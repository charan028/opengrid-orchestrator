"""Shared Jinja2 environment for every screen (`base.html` contract, BUILD.md UI brief). Owner: ui-a.

`NAV_SCREENS` is the fixed left-navigation list every template's `base.html` renders -- the 7 screens at
their fixed paths (BUILD.md UI brief / 02b S8), always relative to `BASE_PATH` (`/og`, matching
`[ui].base_path`'s default, 02b S1.4).
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

BASE_PATH = "/og"

TEMPLATES_DIR = Path(__file__).parent / "templates"

NAV_SCREENS: tuple[dict[str, str], ...] = (
    {"label": "Control room", "path": f"{BASE_PATH}/"},
    {"label": "Fleet", "path": f"{BASE_PATH}/fleet"},
    {"label": "Dispatch", "path": f"{BASE_PATH}/dispatch"},
    {"label": "Markets", "path": f"{BASE_PATH}/markets"},
    {"label": "Health", "path": f"{BASE_PATH}/health"},
    {"label": "Profitability", "path": f"{BASE_PATH}/profitability"},
    {"label": "Billing & audit", "path": f"{BASE_PATH}/billing"},
)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["nav_screens"] = NAV_SCREENS
templates.env.globals["base_path"] = BASE_PATH
