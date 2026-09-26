"""Host-side safe-stop CLI (K8: "a scoped safe stop works when the engine is down").

    python -m opengrid.safestop.cli engage --scope BANK --ref bank-007 --reason "..." \\
        --operator alice --confirm BANK:bank-007

For an operator on the host when og-api (or the og-safestop daemon itself) is down. It builds the same
`SafestopService` `main.py` does -- stop-only key from `[safestop].key_id` / `load_signing_key`, the
Postgres `stop_event` + trace backends, and its own MQTT connection (process `safestop-cli`, client id
`og-safestop-cli` in production, so the broker never kicks the running daemon's `og-safestop` session) --
and calls `SafestopService.engage`, which writes the SAFE_STOP trace record first, then the
`og.stop_event` row, then publishes the signed retained ENGAGE (K10).

There is deliberately no `release` subcommand: the stop-only key can never sign a RELEASE (K8).
`--confirm` must repeat `<SCOPE>:<ref>` exactly (FLEET: `FLEET:FLEET`) -- a typed guard against a slip;
on mismatch nothing is signed or published.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, get_args
from uuid import UUID

import aiomqtt
import psycopg

from opengrid.platform.config import ConfigError, load_config
from opengrid.platform.db import make_pool
from opengrid.platform.mqtt import build_client, topic
from opengrid.safestop.backend import InitiatorKind
from opengrid.safestop.events import (
    InvalidScopeReferenceError,
    Scope,
    is_safe_scope_ref,
    stop_topic_suffix,
)
from opengrid.safestop.keys import SafestopKeyError, load_signing_key
from opengrid.safestop.main import DEFAULT_KEY_ID, MQTT_PASSWORD_ENV, MQTT_USERNAME
from opengrid.safestop.mqtt_publish import AiomqttStopPublisher, StopPublishError
from opengrid.safestop.pg_backend import PgStopEventBackend, retry_trace_conflict
from opengrid.safestop.service import SafestopService
from opengrid.safestop.trace_backend import PgSafestopTraceBackend
from opengrid.trace import TraceStore

CLI_PROCESS_NAME = "safestop-cli"
FLEET_REF = "FLEET"
#: Marks the initiator as the host CLI: `operator:<id>@host-cli`.
INITIATOR_SUFFIX = "@host-cli"
_OPERATOR_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2


class CliValidationError(ValueError):
    """The arguments parse but must not produce a stop (confirm mismatch, bad ref, ...)."""


@dataclass(frozen=True, slots=True)
class EngageRequest:
    scope: Scope
    scope_ref: str
    reason: str
    initiator_ref: str


@dataclass(frozen=True, slots=True)
class EngageResult:
    stop_id: UUID
    topic_suffix: str


class Engager(Protocol):
    """The one `SafestopService` method the CLI uses (tests pass an in-memory fake or a real service
    over fake backends)."""

    async def engage(
        self,
        scope: Scope,
        scope_ref: str,
        reason: str,
        initiator_ref: str,
        *,
        initiator_kind: InitiatorKind = ...,
    ) -> UUID: ...


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m opengrid.safestop.cli",
        description="Engage a scoped safe stop from the host (stop-only key). There is no release.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    engage = sub.add_parser("engage", help="sign and publish a retained ENGAGE stop")
    engage.add_argument("--scope", required=True, choices=get_args(Scope))
    engage.add_argument("--ref", default=None, help="zone/bank id; FLEET: omit or 'FLEET'")
    engage.add_argument("--reason", required=True, help="why (recorded in the stop and the trace)")
    engage.add_argument("--operator", required=True, help="operator id, e.g. alice")
    engage.add_argument(
        "--confirm", required=True, help="type <SCOPE>:<ref> exactly, e.g. BANK:bank-007 or FLEET:FLEET"
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """argparse step: exits (status 2) on an unknown subcommand such as `release` or a missing arg."""
    return build_parser().parse_args(argv)


def validate(args: argparse.Namespace) -> EngageRequest:
    """Pure: turn parsed args into an `EngageRequest`, or raise `CliValidationError`. Nothing is signed
    or published unless this returns."""
    scope: Scope = args.scope
    ref = (args.ref or "").strip()
    if scope == "FLEET":
        if ref not in ("", FLEET_REF):
            raise CliValidationError(f"--ref for FLEET must be omitted or {FLEET_REF!r}, got {ref!r}")
        ref = FLEET_REF
    elif not ref:
        raise CliValidationError(f"--ref is required for --scope {scope}")
    elif not is_safe_scope_ref(ref):
        raise CliValidationError(f"--ref {ref!r} is not a valid {scope.lower()} id")

    expected = f"{scope}:{ref}"
    if args.confirm != expected:
        raise CliValidationError(f"--confirm must be exactly {expected!r}, got {args.confirm!r}")

    reason = args.reason.strip()
    if not reason:
        raise CliValidationError("--reason must not be empty")
    operator = args.operator.strip()
    if not _OPERATOR_RE.fullmatch(operator):
        raise CliValidationError(f"--operator {operator!r} is not a valid operator id")

    return EngageRequest(
        scope=scope,
        scope_ref=ref,
        reason=reason,
        initiator_ref=f"operator:{operator}{INITIATOR_SUFFIX}",
    )


async def run_engage(request: EngageRequest, service: Engager) -> EngageResult:
    """Engage through `SafestopService.engage` (trace -> stop_event row -> retained publish), retrying
    only a trace-append race with the running daemon (nothing written yet at that point)."""
    stop_id = await retry_trace_conflict(
        lambda: service.engage(
            request.scope,
            request.scope_ref,
            request.reason,
            request.initiator_ref,
            initiator_kind="OPERATOR",
        )
    )
    return EngageResult(stop_id, stop_topic_suffix(request.scope, request.scope_ref, stop_id))


async def _engage_live(request: EngageRequest) -> str:
    """Wire the real Postgres/MQTT service exactly as `main.py` does and engage. Returns the full topic."""
    cfg = load_config()
    key_id = str(cfg.get("safestop.key_id", DEFAULT_KEY_ID))
    stop_key = load_signing_key(key_id, cfg)
    pool = await make_pool(cfg)
    try:
        async with build_client(
            cfg,
            username=MQTT_USERNAME,
            password=os.environ.get(MQTT_PASSWORD_ENV, ""),
            process=CLI_PROCESS_NAME,
        ) as client:
            service = SafestopService(
                stop_key,
                PgStopEventBackend(pool),
                AiomqttStopPublisher(client=client, config=cfg),
                TraceStore(PgSafestopTraceBackend(pool)),
            )
            result = await run_engage(request, service)
        full_topic = topic(cfg, result.topic_suffix)
        print(f"stop_id={result.stop_id}")
        print(f"topic={full_topic}")
        return full_topic
    finally:
        await pool.close()


_EXPECTED_ERRORS: tuple[type[BaseException], ...] = (
    ConfigError,
    SafestopKeyError,
    InvalidScopeReferenceError,
    StopPublishError,
    aiomqtt.MqttError,
    psycopg.Error,
    OSError,
    TimeoutError,
)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        request = validate(args)
    except CliValidationError as exc:
        print(f"refused: {exc} -- nothing signed, nothing published", file=sys.stderr)
        return EXIT_REFUSED
    try:
        asyncio.run(_engage_live(request))
    except _EXPECTED_ERRORS as exc:
        print(
            f"error: safe stop NOT confirmed published ({type(exc).__name__}: {exc}). A trace or "
            "stop_event record may exist; re-run to publish a fresh stop.",
            file=sys.stderr,
        )
        return EXIT_FAILED
    print(f"safe stop ENGAGED: {request.scope} {request.scope_ref} by {request.initiator_ref}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
