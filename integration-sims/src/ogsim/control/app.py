"""FastAPI app for ogsim.control: REST API + minimal server-rendered web UI.

Owns the single Injector (shared by manual/UI/CLI, scenario, and random-mode
injections) and the single RandomEngine (autonomous anomaly generation),
so there is exactly one place each anomaly type's behaviour is dispatched
from, per BUILD.md's "single control plane for all simulators."
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from ogsim.control import catalogue
from ogsim.control.injector import InjectionSource, Injector, UnknownAnomalyTypeError
from ogsim.control.random_config import INTENSITY_PROFILES, load_random_config
from ogsim.control.random_engine import RandomEngine
from ogsim.control.scenarios import load_scenarios_dir, run_scenario

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


def create_app(
    scenarios_dir: str | Path | None = None, random_config_path: str | Path | None = None
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
    app.state.random_engine = RandomEngine(app.state.injector, load_random_config(random_config_path))
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

    @app.post("/api/inject", response_model=None)
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
        return {"ok": True, "anomaly": record.to_dict()}

    @app.delete("/api/anomalies/{anomaly_id}")
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

    @app.post("/api/scenarios/{name}/run", response_model=None)
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

    @app.get("/api/scenarios/{name}/status")
    async def scenario_status(request: Request, name: str) -> dict[str, Any]:
        task = request.app.state.running_scenarios.get(name)
        if task is None:
            return {"running": False}
        return {"running": not task.done(), "cancelled": task.cancelled() if task.done() else False}

    @app.get("/api/random/status")
    async def random_status(request: Request) -> dict[str, Any]:
        return random_engine(request).status()

    @app.post("/api/random/pause")
    async def random_pause(request: Request) -> dict[str, Any]:
        random_engine(request).pause()
        return random_engine(request).status()

    @app.post("/api/random/resume")
    async def random_resume(request: Request) -> dict[str, Any]:
        random_engine(request).resume()
        return random_engine(request).status()

    @app.post("/api/random/profile", response_model=None)
    async def random_profile(request: Request, body: ProfileBody) -> JSONResponse | dict[str, Any]:
        if body.profile not in INTENSITY_PROFILES:
            return JSONResponse(status_code=422, content={"error": f"unknown profile '{body.profile}'"})
        random_engine(request).set_profile(body.profile)
        return random_engine(request).status()

    @app.post("/api/random/sims/{sim}")
    async def random_sim_enabled(request: Request, sim: str, body: SimEnabledBody) -> dict[str, Any]:
        random_engine(request).set_sim_enabled(sim, body.enabled)
        return random_engine(request).status()

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        scenarios = load_scenarios_dir(request.app.state.scenarios_dir)
        return templates.TemplateResponse(
            request,
            "index.html",
            {"types": catalogue.as_list(), "scenarios": scenarios, "profiles": INTENSITY_PROFILES},
        )

    return app


app = create_app()
