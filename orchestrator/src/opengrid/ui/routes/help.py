"""The Help page (`/og/help`, release r3.4): a complete, read-only guide to the platform and every screen.

Static by design: the prose lives in `templates/help/*.html` (reviewed against the code for the release),
and the API reference is generated from the running application's own routes, so it can never drift from
what this process actually serves. No database call and no `opengrid.api` HTTP call is made, so the page
renders even while the API's data sources are down. Viewer role: nothing on it writes.

Every screen links here from the last entry of the left navigation (`base.html`), deep-linked to that
screen's section (`opengrid.ui.templating.help_href`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.dependencies.models import Dependant
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute

from opengrid.ui.role import role_of
from opengrid.ui.templating import BASE_PATH, templates

router = APIRouter(prefix="/help")

API_PREFIX = "/og/api"
OPENAPI_PATH = f"{API_PREFIX}/openapi.json"
DOCS_PATH = f"{API_PREFIX}/docs"

#: The table of contents, in page order: (section id, title, sub-entries).
TOC: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    (
        "overview",
        "Overview",
        (("ov-what", "What OpenGrid does"), ("ov-guarantees", "Guarantees K1-K15"), ("ov-roles", "Roles")),
    ),
    (
        "architecture",
        "Architecture",
        (("arch-system", "System diagram"), ("arch-functional", "Functional flow")),
    ),
    ("data-model", "Data model", (("dm-er", "Entity diagram"),)),
    (
        "how-it-works",
        "How it works",
        (
            ("hw-cycle", "Decision cycle"),
            ("hw-lock", "Commitments and the lock"),
            ("hw-holds", "Holds"),
            ("hw-manual", "Manual targets"),
            ("hw-safestop", "Safe stop"),
            ("hw-market", "Market model"),
        ),
    ),
    (
        "pages",
        "Screens",
        (
            ("page-story", "Story"),
            ("page-control-room", "Control room"),
            ("page-fleet", "Fleet"),
            ("page-dispatch", "Dispatch"),
            ("page-markets", "Markets"),
            ("page-health", "System Health"),
            ("page-profitability", "Profitability"),
            ("page-billing", "Billing & audit"),
            ("page-pq", "Power quality"),
            ("page-scenarios", "Scenarios"),
            ("page-help", "Help"),
        ),
    ),
    ("integrations", "Integrations", ()),
    ("apis", "APIs", ()),
    (
        "events",
        "Events and errors",
        (
            ("ev-mqtt", "MQTT topics"),
            ("ev-alerts", "Alert rules"),
            ("ev-degraded", "Degraded modes"),
            ("ev-guardian", "Guardian codes"),
            ("ev-k7", "K7 escalation"),
            ("ev-broker", "Broker reconnect"),
            ("ev-trace", "Trace journal"),
            ("ev-outbox", "Stop outbox"),
        ),
    ),
    (
        "maintenance",
        "Maintenance",
        (
            ("mt-release", "Release and rollback"),
            ("mt-lifecycle", "Data lifecycle"),
            ("mt-firmware", "Firmware"),
            ("mt-logs", "Logs"),
        ),
    ),
    ("playbooks", "Playbooks", (("pb-personas", "Personas"),)),
    ("glossary", "Glossary", ()),
)

#: The prose fragments under `templates/help/`, in page order. `pages-a`/`pages-b` are wrapped by the
#: template's own `pages` section.
FRAGMENTS_BEFORE_PAGES: tuple[str, ...] = ("overview", "architecture", "data-model", "how-it-works")
PAGE_FRAGMENTS: tuple[str, ...] = ("pages-a", "pages-b")
FRAGMENTS_AFTER_PAGES: tuple[str, ...] = ("integrations",)
FRAGMENTS_AFTER_APIS: tuple[str, ...] = ("events", "maintenance", "playbooks", "glossary")

#: The auth dependencies of `opengrid.api.auth` / `opengrid.customer_api`, strongest first, and how the
#: API reference names them.
_ROLE_DEPENDENCIES: tuple[tuple[str, str], ...] = (
    ("require_operator", "operator"),
    ("require_customer", "customer key"),
    ("require_viewer", "viewer"),
    ("require_loopback_health_probe", "local probe"),
    ("current_identity", "signed-in"),
)

_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})


def _dependency_names(dependant: Dependant) -> set[str]:
    names: set[str] = set()
    for sub in dependant.dependencies:
        call: Callable[..., Any] | None = sub.call
        if call is not None:
            names.add(getattr(call, "__name__", ""))
        names |= _dependency_names(sub)
    return names


def route_role(route: APIRoute) -> str:
    """The role an endpoint requires, read from its auth dependencies (not from documentation)."""
    names = _dependency_names(route.dependant)
    for dependency, label in _ROLE_DEPENDENCIES:
        if dependency in names:
            return label
    return "none declared"


def iter_api_routes(routes: Iterable[Any], prefix: str = "") -> Iterator[tuple[str, APIRoute]]:
    """Every `APIRoute` under `routes` with its full path, whether FastAPI keeps included routers flattened
    (older releases) or as lazily-resolved included-router entries (newer releases)."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
            continue
        original = getattr(route, "original_router", None)
        context = getattr(route, "include_context", None)
        if original is not None:
            yield from iter_api_routes(original.routes, prefix + str(getattr(context, "prefix", "") or ""))


def _first_paragraph(text: str | None) -> str:
    return " ".join((text or "").strip().split("\n\n", 1)[0].split())


def _purpose(route: APIRoute) -> str:
    if route.summary:
        return route.summary
    doc = _first_paragraph(route.description or getattr(route.endpoint, "__doc__", None))
    return doc or route.name.replace("_", " ").capitalize()


def _slug(name: str) -> str:
    return "api-" + "".join(c if c.isalnum() else "-" for c in name.lower())


def api_reference(app: FastAPI) -> list[dict[str, Any]]:
    """Every endpoint `/og/api/openapi.json` publishes, grouped by tag (or first path segment), each with
    its method, path, required role (from the route's auth dependency) and purpose."""
    roles: dict[tuple[str, str], str] = {}
    for path, route in iter_api_routes(app.routes):
        for method in route.methods or ():
            roles[(path, method.upper())] = route_role(route)
    groups: dict[str, list[dict[str, str]]] = {}
    paths: dict[str, Any] = app.openapi().get("paths", {})
    for path, operations in paths.items():
        if not path.startswith(API_PREFIX) or not isinstance(operations, dict):
            continue
        for method, op in operations.items():
            if method.upper() not in _HTTP_METHODS or not isinstance(op, dict):
                continue
            tags = op.get("tags") or []
            group = str(tags[0]) if tags else (path[len(API_PREFIX) :].strip("/").split("/", 1)[0] or "api")
            purpose = _first_paragraph(op.get("description")) or str(op.get("summary") or "")
            groups.setdefault(group, []).append(
                {
                    "methods": method.upper(),
                    "path": path,
                    "role": roles.get((path, method.upper()), "none declared"),
                    "summary": str(op.get("summary") or ""),
                    "purpose": purpose,
                    "direction": "outbound (SSE push)"
                    if path.startswith(f"{API_PREFIX}/stream")
                    else "inbound",
                }
            )
    return [
        {
            "name": name,
            "slug": _slug(name),
            "endpoints": sorted(rows, key=lambda row: (row["path"], row["methods"])),
        }
        for name, rows in sorted(groups.items())
    ]


def console_routes(app: FastAPI, base_path: str) -> list[dict[str, str]]:
    """The console's own (HTML/HTMX) routes under `base_path`, outside `/og/api`."""
    rows: list[dict[str, str]] = []
    for path, route in iter_api_routes(app.routes):
        if path.startswith(API_PREFIX) or not path.startswith(base_path):
            continue
        rows.append(
            {
                "methods": ", ".join(sorted(m for m in route.methods or () if m != "HEAD")),
                "path": path,
                "purpose": _purpose(route),
            }
        )
    return sorted(rows, key=lambda row: (row["path"], row["methods"]))


@router.get("", response_class=HTMLResponse)
async def help_page(request: Request) -> HTMLResponse:
    """The Help page: overview, architecture, data model, every screen, integrations, APIs, events and
    errors, maintenance, playbooks and glossary."""
    app: FastAPI = request.app
    groups = api_reference(app)
    return templates.TemplateResponse(
        request,
        "help.html",
        {
            "role": role_of(request),
            "toc": TOC,
            "fragments_before_pages": FRAGMENTS_BEFORE_PAGES,
            "page_fragments": PAGE_FRAGMENTS,
            "fragments_after_pages": FRAGMENTS_AFTER_PAGES,
            "fragments_after_apis": FRAGMENTS_AFTER_APIS,
            "api_groups": groups,
            "api_endpoint_count": sum(len(group["endpoints"]) for group in groups),
            "console_routes": console_routes(app, BASE_PATH),
            "openapi_path": OPENAPI_PATH,
            "docs_path": DOCS_PATH,
        },
    )
