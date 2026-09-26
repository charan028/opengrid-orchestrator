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

Utility L2 intake (K5/K8): a background task (`l2_intake.run_l2_instruction_listener`, its own MQTT
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

import opengrid.safestop as safestop
from opengrid.platform.config import Config, load_config
from opengrid.platform.db import make_pool
from opengrid.platform.heartbeat import write_heartbeat
from opengrid.platform.log import configure_logging
from opengrid.platform.mqtt import build_client
from opengrid.platform.process import run_forever
from opengrid.safestop.confirmation import (
    ConfirmationBroker,
    ProposalExpiredError,
    UnknownProposalError,
)
from opengrid.safestop.keys import load_signing_key
from opengrid.safestop.l2_intake import run_l2_instruction_listener
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

    async with build_client(
        cfg, username=mqtt_username, password=mqtt_password, process="safestop"
    ) as client:
        publisher = AiomqttStopPublisher(client=client, config=cfg)
        service = SafestopService(
            stop_key, backend, publisher, trace, guardian_public_key=load_guardian_public_key(cfg)
        )
        safestop.configure_service(service)
        release_retain_s = float(cfg.get("safestop.release_retain_s", DEFAULT_RELEASE_RETAIN_S))

        intake_task = asyncio.create_task(_request_intake_loop(pool, broker))

        async def _l2_engage(bank_id: str, reason: str, initiator_ref: str) -> UUID:
            return await retry_trace_conflict(
                lambda: service.engage("BANK", bank_id, reason, initiator_ref, initiator_kind="UTILITY")
            )

        l2_task = asyncio.create_task(
            run_l2_instruction_listener(
                cfg,
                username=mqtt_username,
                password=mqtt_password,
                engage_fn=_l2_engage,
                already_acted_fn=backend.has_l2_engage,
            )
        )
        try:

            async def _tick() -> None:
                broker.discard_expired()
                await write_heartbeat(pool, PROCESS_NAME)
                try:
                    await service.clear_released_retained(backend, retain_s=release_retain_s)
                except Exception:
                    logger.exception("retained stop-topic housekeeping failed; retried next tick")

            await run_forever(_tick, interval_s=heartbeat_interval_s, process_name=PROCESS_NAME)
        finally:
            for task in (intake_task, l2_task):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await pool.close()
            safestop.configure_service(None)


def _entrypoint() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    _entrypoint()
