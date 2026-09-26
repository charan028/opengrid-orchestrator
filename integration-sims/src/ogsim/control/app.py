"""FastAPI app for ogsim.control: REST API + minimal server-rendered web UI.

Owns the single Injector (shared by manual/UI/CLI, scenario, and random-mode
injections) and the single RandomEngine (autonomous anomaly generation),
so there is exactly one place each anomaly type's behaviour is dispatched
from, per BUILD.md's "single control plane for all simulators."
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from ogsim.control import catalogue
from ogsim.control.injector import InjectionSource, Injector, UnknownAnomalyTypeError
from ogsim.control.random_config import INTENSITY_PROFILES, load_random_config
from ogsim.control.random_engine import RandomEngine
from ogsim.control.scenarios import load_scenarios_dir, run_scenario, stop_scenario_anomalies
from ogsim.control.schema_validation import ScenarioCmdValidationError

TEMPLATES_DIR = Path(__file__).parent / "templates"
DEFAULT_SCENARIOS_DIR = Path(__file__).resolve().parents[3] / "scenarios"


class InjectBody(BaseModel):
    type: str
    target: str
    params: dict[str, Any] = {}
    duration: float = 60.0
    id: str | None = None
    start: float | None = None


class ScenarioRunBody(BaseModel):
    speed: float = 1.0


class ProfileBody(BaseModel):
    profile: str


class SimEnabledBody(BaseModel):
    enabled: bool


#: Env var override for a fixed deployment prefix (e.g. Apache ProxyPass at "/ogsim"), when no
#: X-Forwarded-Prefix header is set by the reverse proxy. Live bug fix, 2026-09-26 (FLEET-SIM demo
#: gap #17): the web UI's own JS called `/api/...` by ABSOLUTE path, which only ever works when the
#: page is served from the domain root -- behind Apache's `/ogsim/` ProxyPass, every button called the
#: wrong (unprefixed) URL and 404'd.
BASE_PATH_ENV = "OGSIM_CONTROL_BASE_PATH"

#: R3.1 LOW-review fix, 2026-09-26: every state-changing endpoint below (POST/DELETE) had no CSRF
#: check -- there's no cookie-based auth here, but the operator's browser session still has no
#: protection against a third-party page silently issuing these requests. Requiring this custom
#: header blocks a plain cross-site <form>/fetch: a browser refuses to let cross-origin JS set an
#: arbitrary header without a CORS preflight, and this app grants no CORS origin, so only same-origin
#: JS (the control page's own fetchJson(), templates/index.html) can ever send it.
CSRF_HEADER_NAME = "x-ogsim-request"


def _require_csrf_header(request: Request) -> None:
    if request.headers.get(CSRF_HEADER_NAME) != "1":
        raise HTTPException(status_code=403, detail=f"missing/invalid {CSRF_HEADER_NAME} header")


def _base_path(request: Request) -> str:
    """The deployment path prefix this app is mounted under, with no trailing slash (`""` at the
    domain root). `X-Forwarded-Prefix` (set by a reverse proxy that strips its own mount prefix before
    forwarding, e.g. Apache `ProxyPass /ogsim http://... ProxyPassReverse` plus
    `RequestHeader set X-Forwarded-Prefix /ogsim`) wins; else `OGSIM_CONTROL_BASE_PATH`; else `""`."""
    forwarded = request.headers.get("x-forwarded-prefix", "").strip()
    if forwarded:
        return forwarded.rstrip("/")
    return os.environ.get(BASE_PATH_ENV, "").rstrip("/")


def create_app(
    scenarios_dir: str | Path | None = None,
    random_config_path: str | Path | None = None,
    random_pause_state_path: str | Path | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.random_engine.start()
        try:
            yield
        finally:
            app.state.random_engine.stop()

    app = FastAPI(
        title="ogsim.control",
        description="Anomaly injection control plane for OpenGrid integration sims",
        lifespan=lifespan,
    )
    running_scenarios: dict[str, asyncio.Task[list[str]]] = {}
    app.state.injector = Injector()
    app.state.scenarios_dir = Path(scenarios_dir) if scenarios_dir else DEFAULT_SCENARIOS_DIR
    app.state.running_scenarios = running_scenarios
    app.state.random_engine = RandomEngine(
        app.state.injector,
        load_random_config(random_config_path),
        pause_state_path=str(random_pause_state_path) if random_pause_state_path else None,
    )
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def injector(request: Request) -> Injector:
        return request.app.state.injector

    def random_engine(request: Request) -> RandomEngine:
        return request.app.state.random_engine

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/anomaly-types")
    async def anomaly_types() -> dict[str, Any]:
        return {"types": catalogue.as_list()}

    @app.get("/api/anomalies")
    async def list_anomalies(request: Request, source: InjectionSource | None = None) -> dict[str, Any]:
        return {"active": [r.to_dict() for r in injector(request).active(source=source)]}

    @app.post("/api/inject", response_model=None, dependencies=[Depends(_require_csrf_header)])
    async def inject(request: Request, body: InjectBody) -> JSONResponse | dict[str, Any]:
        try:
            record = await injector(request).inject(
                type_=body.type,
                target=body.target,
                params=body.params,
                duration=body.duration,
                start=body.start,
                anomaly_id=body.id,
                source="manual",
            )
        except UnknownAnomalyTypeError as exc:
            return JSONResponse(status_code=422, content={"error": str(exc)})
        except ScenarioCmdValidationError as exc:
            # Defense in depth (post-deploy defect: a catalogue entry whose wire_type was not
            # yet in scenario_control.schema.json's enum returned a raw 500 here) -- a schema
            # mismatch is a client-correctable request shape issue, not a server fault.
            return JSONResponse(status_code=422, content={"error": str(exc)})
        return {"ok": True, "anomaly": record.to_dict()}

    @app.delete("/api/anomalies/{anomaly_id}", dependencies=[Depends(_require_csrf_header)])
    async def cancel(request: Request, anomaly_id: str) -> dict[str, bool]:
        found = await injector(request).cancel(anomaly_id)
        return {"ok": found}

    @app.get("/api/log")
    async def log(request: Request) -> dict[str, Any]:
        return {"log": injector(request).injection_log()}

    @app.get("/api/scenarios")
    async def list_scenarios(request: Request) -> dict[str, Any]:
        scenarios = load_scenarios_dir(request.app.state.scenarios_dir)
        return {
            "scenarios": [
                {"name": s.name, "description": s.description.strip(), "steps": len(s.steps), "path": s.path}
                for s in scenarios
            ]
        }

    @app.post("/api/scenarios/{name}/run", response_model=None, dependencies=[Depends(_require_csrf_header)])
    async def run_named_scenario(
        request: Request, name: str, body: ScenarioRunBody
    ) -> JSONResponse | dict[str, Any]:
        scenarios = load_scenarios_dir(request.app.state.scenarios_dir)
        match = next((s for s in scenarios if s.name == name), None)
        if match is None:
            return JSONResponse(status_code=404, content={"error": f"unknown scenario '{name}'"})
        running = request.app.state.running_scenarios
        if name in running and not running[name].done():
            return JSONResponse(status_code=409, content={"error": f"scenario '{name}' is already running"})
        running[name] = asyncio.create_task(run_scenario(injector(request), match, speed=body.speed))
        return {"ok": True, "scenario": name, "steps": len(match.steps)}

    @app.post("/api/scenarios/{name}/stop", response_model=None, dependencies=[Depends(_require_csrf_header)])
    async def stop_named_scenario(request: Request, name: str) -> JSONResponse | dict[str, Any]:
        """Demo gap #18, 2026-09-26: cancels `name`'s pending steps (if its scenario task is still
        running) AND ends every anomaly it already injected -- `run_named_scenario`'s asyncio task
        alone has no "stop" verb, and cancelling just the task leaves any already-injected anomaly
        (e.g. a long `duration` bank_overload) running for its full original duration."""
        scenarios = load_scenarios_dir(request.app.state.scenarios_dir)
        if not any(s.name == name for s in scenarios):
            return JSONResponse(status_code=404, content={"error": f"unknown scenario '{name}'"})
        task = request.app.state.running_scenarios.get(name)
        task_cancelled = task is not None and not task.done()
        if task_cancelled:
            task.cancel()
        ended = await stop_scenario_anomalies(injector(request), name)
        return {"ok": True, "scenario": name, "task_cancelled": task_cancelled, "anomalies_ended": ended}

    @app.post("/api/scenarios/stop-all", dependencies=[Depends(_require_csrf_header)])
    async def stop_all_scenarios(request: Request) -> dict[str, Any]:
        """Demo gap #18: stops every scenario this process has ever run (its task, if still running,
        plus every still-active anomaly it injected) -- not limited to scenarios currently tracked as
        "running", since a scenario's own anomalies can outlive its task (its steps finish injecting
        well before their last `duration` elapses)."""
        names = {r.id.split(":", 1)[0] for r in injector(request).active(source="scenario")}
        names |= set(request.app.state.running_scenarios)
        stopped = []
        for name in sorted(names):
            task = request.app.state.running_scenarios.get(name)
            task_cancelled = task is not None and not task.done()
            if task_cancelled:
                task.cancel()
            ended = await stop_scenario_anomalies(injector(request), name)
            stopped.append({"scenario": name, "task_cancelled": task_cancelled, "anomalies_ended": ended})
        return {"ok": True, "stopped": stopped}

    @app.get("/api/scenarios/{name}/status")
    async def scenario_status(request: Request, name: str) -> dict[str, Any]:
        task = request.app.state.running_scenarios.get(name)
        if task is None:
            return {"running": False}
        return {"running": not task.done(), "cancelled": task.cancelled() if task.done() else False}

    @app.get("/api/random/status")
    async def random_status(request: Request) -> dict[str, Any]:
        return random_engine(request).status()

    @app.post("/api/random/pause", dependencies=[Depends(_require_csrf_header)])
    async def random_pause(request: Request) -> dict[str, Any]:
        random_engine(request).pause()
        return random_engine(request).status()

    @app.post("/api/random/resume", dependencies=[Depends(_require_csrf_header)])
    async def random_resume(request: Request) -> dict[str, Any]:
        random_engine(request).resume()
        return random_engine(request).status()

    @app.post("/api/random/profile", response_model=None, dependencies=[Depends(_require_csrf_header)])
    async def random_profile(request: Request, body: ProfileBody) -> JSONResponse | dict[str, Any]:
        if body.profile not in INTENSITY_PROFILES:
            return JSONResponse(status_code=422, content={"error": f"unknown profile '{body.profile}'"})
        random_engine(request).set_profile(body.profile)
        return random_engine(request).status()

    @app.post("/api/random/sims/{sim}", dependencies=[Depends(_require_csrf_header)])
    async def random_sim_enabled(request: Request, sim: str, body: SimEnabledBody) -> dict[str, Any]:
        random_engine(request).set_sim_enabled(sim, body.enabled)
        return random_engine(request).status()

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        scenarios = load_scenarios_dir(request.app.state.scenarios_dir)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "types": catalogue.as_list(),
                "scenarios": scenarios,
                "profiles": INTENSITY_PROFILES,
                "base_path": _base_path(request),
            },
        )

    return app


app = create_app()
