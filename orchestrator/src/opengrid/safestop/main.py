"""Process entry point for `og-safestop` (BUILD.md S4, 02b S1.2).

Deliberately the smallest, most dependency-free of the seven processes (02b S1.2's 256M footprint
note): it imports only `opengrid.core`, `opengrid.platform` and `opengrid.trace`, opens its own
Postgres pool and its own MQTT connection, and never imports `opengrid.engine` or `opengrid.guardian`
(K8). `tests/unit/safestop/test_import_isolation.py` asserts this with an import-graph check.

Request intake (02b S8's two-step `POST /og/api/safestop` then `/confirm`): `og-api` NOTIFYs on
`opengrid.safestop.pg_backend.REQUEST_CHANNEL` with a JSON payload `{"action": "PROPOSE"|"CONFIRM",
"proposal_id": <uuid>, "scope": ..., "scope_ref": ..., "reason": ..., "initiator_ref": ...}`
(PROPOSE) or `{"action": "CONFIRM", "proposal_id": <uuid>}` (CONFIRM). `og-safestop` only calls
`engage()` after a matching CONFIRM arrives within `confirm_window_s` of the PROPOSE -- a single
message can never stop the fleet (TS-10-03).

Utility L2 intake (K5/K8): a background task (`l2_intake.build_l2_session`, its own MQTT
connection `safestop-l2`) engages a BANK stop on every unexpired BLOCK/ESTOP `ScadaUtilityInstruction`,
once per instruction id.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path
from typing import Any
from uuid import UUID

from prometheus_client import start_http_server

import opengrid.safestop as safestop
from opengrid.platform.config import Config, load_config
from opengrid.platform.db import make_pool
from opengrid.platform.heartbeat import write_heartbeat
from opengrid.platform.log import configure_logging
from opengrid.platform.mqtt import build_client
from opengrid.platform.mqtt_session import MqttSession
from opengrid.platform.process import run_forever
from opengrid.safestop.confirmation import (
    ConfirmationBroker,
    ProposalExpiredError,
    UnknownProposalError,
)
from opengrid.safestop.keys import load_signing_key
from opengrid.safestop.l2_intake import DEFAULT_MAX_BACKOFF_S as L2_MAX_BACKOFF_S
from opengrid.safestop.l2_intake import DEFAULT_RECONNECT_DELAY_S as L2_MIN_BACKOFF_S
from opengrid.safestop.l2_intake import build_l2_session
from opengrid.safestop.mqtt_publish import AiomqttStopPublisher
from opengrid.safestop.pg_backend import PgStopEventBackend, listen_for_requests, retry_trace_conflict
from opengrid.safestop.service import SafestopService
from opengrid.safestop.trace_backend import PgSafestopTraceBackend
from opengrid.trace import TraceStore

logger = logging.getLogger("opengrid.safestop.main")

PROCESS_NAME = "safestop"
DEFAULT_KEY_ID = "safestop-2026a"
MQTT_USERNAME = "og_safestop"
MQTT_PASSWORD_ENV = "OG_MQTT_SAFESTOP_PASSWORD"  # noqa: S105 -- an env-var name, not a secret
DEFAULT_HEARTBEAT_INTERVAL_S = 5.0
DEFAULT_GUARDIAN_PUBLIC_KEY_PATH = "/etc/opengrid/guardian_ed25519.pub"
GUARDIAN_PUBLIC_KEY_LENGTH = 32
#: K8: how long a relayed RELEASE stays retained before its topic is cleared. Must exceed the longest a
#: hub may be offline and still hold its in-memory stop set; after that a reconnecting hub that lost its
#: memory starts unstopped anyway, which is correct once the stop is released.
DEFAULT_RELEASE_RETAIN_S = 86_400.0
#: K8 fail closed: seconds the stop publish connection (or the L2 listener) may stay down before the process exits for
#: a restart -- the guardian's value (r3.4.4 live: at 30 s every ~15-20 s broker blip restarted og-safestop).
DEFAULT_MQTT_DOWN_EXIT_S = 60.0
#: og-safestop's /metrics port (02b S1.2's process table), e.g. og_mqtt_reconnects_total{client="safestop"}.
DEFAULT_METRICS_PORT = 9106


async def _handle_request(payload: dict[str, Any], broker: ConfirmationBroker) -> None:
    action = payload.get("action")
    if action == "PROPOSE":
        proposal_id = UUID(payload["proposal_id"]) if payload.get("proposal_id") else None
        broker.propose(
            scope=payload["scope"],
            scope_ref=payload.get("scope_ref") or "",
            reason=payload["reason"],
            initiator_ref=payload["initiator_ref"],
            proposal_id=proposal_id,
        )
        return

    if action == "CONFIRM":
        try:
            proposal = broker.confirm(UUID(payload["proposal_id"]))
        except (UnknownProposalError, ProposalExpiredError) as exc:
            logger.warning("safe-stop confirmation refused", extra={"error": str(exc)})
            return
        await safestop.engage(proposal.scope, proposal.scope_ref, proposal.reason, proposal.initiator_ref)
        return

    if action == "PUBLISH_RELEASE":
        # From og-guardian only in practice, but trusted by nothing: the relay verifies the guardian
        # signature and Tier-2 fields itself, and the stop-only key signs nothing (K8).
        event = payload.get("event")
        await safestop.relay_guardian_release(event if isinstance(event, dict) else {})
        return

    logger.warning("unrecognised safestop request action", extra={"action": action})


def load_guardian_public_key(cfg: Config) -> bytes | None:
    """The guardian's public key (hex file, `[safestop].guardian_public_key_path`, default the same file
    the hub simulators verify with). Unreadable or malformed -> None: RELEASE relay stays disabled."""
    path = Path(str(cfg.get("safestop.guardian_public_key_path", DEFAULT_GUARDIAN_PUBLIC_KEY_PATH)))
    try:
        key = bytes.fromhex(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        logger.warning(
            "guardian public key unavailable; stop RELEASE relay disabled", extra={"path": str(path)}
        )
        return None
    if len(key) != GUARDIAN_PUBLIC_KEY_LENGTH:
        logger.warning(
            "guardian public key malformed; stop RELEASE relay disabled", extra={"path": str(path)}
        )
        return None
    return key


async def _request_intake_loop(pool: Any, broker: ConfirmationBroker) -> None:
    """Runs for the lifetime of the process (own task, not the `run_forever` tick) so it can block on
    the next NOTIFY instead of polling."""
    async for payload in listen_for_requests(pool):
        try:
            await _handle_request(payload, broker)
        except (KeyError, ValueError) as exc:
            logger.warning(
                "malformed safestop request ignored", extra={"error": str(exc), "payload": payload}
            )
        except Exception:
            # One failed request (a database error, a publish problem) must never end the intake task: every
            # later stop request would then go unheard while the process looks healthy (r3.4.1 review).
            logger.exception(
                "safestop request failed; intake continues", extra={"action": payload.get("action")}
            )


async def main(cfg: Config | None = None) -> None:
    configure_logging(PROCESS_NAME)
    cfg = cfg or load_config()

    key_id = str(cfg.get("safestop.key_id", DEFAULT_KEY_ID))
    stop_key = load_signing_key(key_id, cfg)

    pool = await make_pool(cfg)
    mqtt_username = MQTT_USERNAME
    mqtt_password = os.environ.get(MQTT_PASSWORD_ENV, "")

    backend = PgStopEventBackend(pool)
    trace = TraceStore(PgSafestopTraceBackend(pool))
    broker = ConfirmationBroker(
        confirm_window_s=int(cfg.get("safestop.confirm_window_s", 30)),
    )

    heartbeat_interval_s = float(cfg.get("health.heartbeat_interval_s", DEFAULT_HEARTBEAT_INTERVAL_S))
    start_metrics_server(cfg)

    # K8: the stop publish connection survives broker disconnects (reconnect with backoff under the same
    # client id, never two clients at once); while it is down the heartbeat stops, and past
    # `safestop.mqtt_down_exit_s` the process exits so systemd restarts it.
    client = MqttSession(
        lambda: build_client(cfg, username=mqtt_username, password=mqtt_password, process="safestop"),
        name="safestop",
        min_backoff_s=L2_MIN_BACKOFF_S,  # the stop path retries fast: 1 s, at most 5 s apart
        max_backoff_s=L2_MAX_BACKOFF_S,
    )
    mqtt_down_exit_s = float(cfg.get("safestop.mqtt_down_exit_s", DEFAULT_MQTT_DOWN_EXIT_S))
    publisher = AiomqttStopPublisher(client=client, config=cfg)
    service = SafestopService(
        stop_key, backend, publisher, trace, guardian_public_key=load_guardian_public_key(cfg), outbox=backend
    )
    # K8 durable outbox: every (re)connect re-publishes, in order, whatever the broker has not acknowledged
    # yet -- including stops engaged while it was down, and anything queued before a restart.
    client.on_connect = service.drain_outbox
    client_task = asyncio.create_task(client.run())
    safestop.configure_service(service)
    release_retain_s = float(cfg.get("safestop.release_retain_s", DEFAULT_RELEASE_RETAIN_S))

    intake_task = asyncio.create_task(_request_intake_loop(pool, broker))

    async def _l2_engage(bank_id: str, reason: str, initiator_ref: str) -> UUID:
        return await retry_trace_conflict(
            lambda: service.engage("BANK", bank_id, reason, initiator_ref, initiator_kind="UTILITY")
        )

    l2_session = build_l2_session(
        cfg,
        username=mqtt_username,
        password=mqtt_password,
        engage_fn=_l2_engage,
        already_acted_fn=backend.has_l2_engage,
        ensure_published_fn=service.ensure_l2_engage_published,
    )
    l2_task = asyncio.create_task(l2_session.run())
    try:

        async def _tick() -> None:
            broker.discard_expired()
            if not stop_path_ready(client, l2_session, exit_after_s=mqtt_down_exit_s):
                return  # no heartbeat while stops cannot be published (health sees safestop down)
            await write_heartbeat(pool, PROCESS_NAME)
            try:
                await service.drain_outbox()  # backstop: anything still queued (e.g. a publish mid-drop)
            except Exception:
                logger.exception("stop outbox drain failed; retried next tick")
            try:
                await service.clear_released_retained(backend, retain_s=release_retain_s)
            except Exception:
                logger.exception("retained stop-topic housekeeping failed; retried next tick")

        await run_forever(_tick, interval_s=heartbeat_interval_s, process_name=PROCESS_NAME)
    finally:
        for task in (intake_task, l2_task, client_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await pool.close()
        safestop.configure_service(None)


def start_metrics_server(cfg: Config) -> int:
    """Serve /metrics on [metrics].safestop_port (default 9106) at [metrics].bind_host (loopback by default),
    like the guardian's [metrics].guardian_port. Returns the port."""
    port = int(cfg.get("metrics.safestop_port", DEFAULT_METRICS_PORT))
    start_http_server(port, addr=str(cfg.get("metrics.bind_host", "127.0.0.1")))
    return port


def stop_path_ready(*sessions: MqttSession, exit_after_s: float) -> bool:
    """K8 fail closed: True while the stop publish connection and the utility L2 listener are both up. While
    one is down (reconnecting) the process writes no heartbeat (health sees og-safestop down); once one has
    been down longer than `exit_after_s` this raises `SystemExit` so systemd restarts og-safestop."""
    down = [s for s in sessions if not s.connected]
    if not down:
        return True
    down_s = max(s.down_for_s() for s in down)
    names = ",".join(s.name for s in down)
    logger.error("safestop MQTT connection down", extra={"clients": names, "down_s": round(down_s, 1)})
    if down_s > exit_after_s:
        raise SystemExit(
            f"og-safestop MQTT connection(s) {names} down for {down_s:.0f}s: exiting for restart"
        )
    return False


def _entrypoint() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    _entrypoint()
