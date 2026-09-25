"""CLI for ogsim.control: inject/cancel anomalies, list types/log, run
scenarios, or serve the REST API + web UI.

Examples:
    python -m ogsim.control serve
    python -m ogsim.control inject --type price_spike --target np6-905-cd \
        --params '{"value_usd_per_mwh": 5000}' --duration 300
    python -m ogsim.control cancel --id <anomaly-id>
    python -m ogsim.control list-active
    python -m ogsim.control list-types
    python -m ogsim.control list-log
    python -m ogsim.control run-scenario integration-sims/scenarios/price_spike_during_delivery.yaml
    python -m ogsim.control random status
    python -m ogsim.control random pause
    python -m ogsim.control random set-profile --profile chaos

`inject`/`cancel`/`list-active`/`random *` talk to a running `ogsim.control
serve` process over its REST API by default (OGSIM_CONTROL_URL, default
http://127.0.0.1:8091), since the active-anomaly registry and the random
engine's state live in that process, not in a one-shot CLI invocation. Pass
`--local` to `inject`/`cancel`/`list-active` to instead build anomalies
directly, in-process (no server needed - useful for scripts/CI, but the
anomaly won't show up in a running server's UI or `/api/anomalies`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

import httpx

from ogsim.control import catalogue
from ogsim.control.injector import Injector, UnknownAnomalyTypeError
from ogsim.control.scenarios import Scenario, load_scenario, run_scenario

DEFAULT_CONTROL_URL = "http://127.0.0.1:8091"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ogsim.control")
    sub = parser.add_subparsers(dest="command", required=True)

    serve_p = sub.add_parser("serve", help="Run the REST API + web UI (default port 8091)")
    serve_p.add_argument("--host", default="0.0.0.0")
    serve_p.add_argument("--port", type=int, default=8091)

    inject_p = sub.add_parser("inject", help="Inject one anomaly (REST by default)")
    inject_p.add_argument("--type", required=True)
    inject_p.add_argument("--target", required=True)
    inject_p.add_argument("--params", default="{}", help="JSON object")
    inject_p.add_argument("--duration", type=float, default=60.0)
    inject_p.add_argument("--id", default=None)
    inject_p.add_argument(
        "--local", action="store_true", help="Build in-process instead of calling the server"
    )

    cancel_p = sub.add_parser("cancel", help="Cancel an active anomaly by id (REST by default)")
    cancel_p.add_argument("--id", required=True)
    cancel_p.add_argument(
        "--local", action="store_true", help="Cancel in-process instead of calling the server"
    )

    sub.add_parser("list-types", help="List the full anomaly catalogue")
    list_active_p = sub.add_parser("list-active", help="List currently active anomalies (REST by default)")
    list_active_p.add_argument(
        "--local", action="store_true", help="Read the in-process registry instead of the server"
    )
    sub.add_parser("list-log", help="Print the injection log (JSONL)")

    scenario_p = sub.add_parser("run-scenario", help="Run a scenario YAML file")
    scenario_p.add_argument("path")
    scenario_p.add_argument("--speed", type=float, default=1.0)

    random_p = sub.add_parser("random", help="Control the autonomous random-mode engine")
    random_sub = random_p.add_subparsers(dest="random_command", required=True)
    random_sub.add_parser("status", help="Show random-mode status")
    random_sub.add_parser("pause", help="Pause all random-mode injection")
    random_sub.add_parser("resume", help="Resume random-mode injection")
    profile_p = random_sub.add_parser("set-profile", help="Set the intensity profile")
    profile_p.add_argument("--profile", required=True, choices=["calm", "normal", "stressed", "chaos"])
    sim_p = random_sub.add_parser("set-sim", help="Enable/disable random mode for one simulator")
    sim_p.add_argument("--sim", required=True, choices=["market", "scada", "fleet"])
    sim_p.add_argument("--enabled", type=lambda v: v.lower() in ("1", "true", "yes", "on"), required=True)

    return parser


async def _run_inject_local(args: argparse.Namespace, params: dict[str, Any]) -> int:
    injector = Injector()
    try:
        record = await injector.inject(
            type_=args.type,
            target=args.target,
            params=params,
            duration=args.duration,
            anomaly_id=args.id,
        )
    except UnknownAnomalyTypeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(record.to_dict(), indent=2))
    return 0


async def _run_inject_rest(args: argparse.Namespace, params: dict[str, Any]) -> int:
    body = {
        "type": args.type,
        "target": args.target,
        "params": params,
        "duration": args.duration,
        "id": args.id,
    }
    async with httpx.AsyncClient(base_url=_control_url(), timeout=10.0) as client:
        resp = await client.post("/api/inject", json=body)
    print(json.dumps(resp.json(), indent=2))
    return 0 if resp.is_success else 2


async def _run_inject(args: argparse.Namespace) -> int:
    try:
        params = json.loads(args.params)
    except json.JSONDecodeError as exc:
        print(f"invalid --params JSON: {exc}", file=sys.stderr)
        return 2
    if args.local:
        return await _run_inject_local(args, params)
    return await _run_inject_rest(args, params)


async def _run_cancel(args: argparse.Namespace) -> int:
    if args.local:
        injector = Injector()
        found = await injector.cancel(args.id)
        print(json.dumps({"ok": found}))
        return 0 if found else 1
    async with httpx.AsyncClient(base_url=_control_url(), timeout=10.0) as client:
        resp = await client.delete(f"/api/anomalies/{args.id}")
    body = resp.json()
    print(json.dumps(body, indent=2))
    return 0 if body.get("ok") else 1


async def _run_list_active(args: argparse.Namespace) -> int:
    if args.local:
        injector = Injector()
        print(json.dumps([r.to_dict() for r in injector.active()], indent=2))
        return 0
    async with httpx.AsyncClient(base_url=_control_url(), timeout=10.0) as client:
        resp = await client.get("/api/anomalies")
    print(json.dumps(resp.json().get("active", []), indent=2))
    return 0 if resp.is_success else 2


async def _run_scenario_cmd(args: argparse.Namespace) -> int:
    scenario: Scenario = load_scenario(args.path)
    injector = Injector()
    ids = await run_scenario(injector, scenario, speed=args.speed)
    print(json.dumps({"scenario": scenario.name, "injected": ids}, indent=2))
    return 0


def _control_url() -> str:
    return os.environ.get("OGSIM_CONTROL_URL", DEFAULT_CONTROL_URL)


async def _run_random_cmd(args: argparse.Namespace) -> int:
    async with httpx.AsyncClient(base_url=_control_url(), timeout=10.0) as client:
        if args.random_command == "status":
            resp = await client.get("/api/random/status")
        elif args.random_command == "pause":
            resp = await client.post("/api/random/pause")
        elif args.random_command == "resume":
            resp = await client.post("/api/random/resume")
        elif args.random_command == "set-profile":
            resp = await client.post("/api/random/profile", json={"profile": args.profile})
        elif args.random_command == "set-sim":
            resp = await client.post(f"/api/random/sims/{args.sim}", json={"enabled": args.enabled})
        else:
            print(f"unknown random subcommand '{args.random_command}'", file=sys.stderr)
            return 2
    print(json.dumps(resp.json(), indent=2))
    return 0 if resp.is_success else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "serve":
        import uvicorn

        uvicorn.run("ogsim.control.app:app", host=args.host, port=args.port, log_level="info")
        return 0
    if args.command == "inject":
        return asyncio.run(_run_inject(args))
    if args.command == "cancel":
        return asyncio.run(_run_cancel(args))
    if args.command == "list-types":
        print(json.dumps(catalogue.as_list(), indent=2))
        return 0
    if args.command == "list-active":
        return asyncio.run(_run_list_active(args))
    if args.command == "list-log":
        injector = Injector()
        for row in injector.injection_log():
            print(json.dumps(row))
        return 0
    if args.command == "run-scenario":
        return asyncio.run(_run_scenario_cmd(args))
    if args.command == "random":
        return asyncio.run(_run_random_cmd(args))
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
