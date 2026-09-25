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
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
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
from opengrid.safestop.mqtt_publish import AiomqttStopPublisher
from opengrid.safestop.pg_backend import PgStopEventBackend, listen_for_requests
from opengrid.safestop.service import SafestopService
from opengrid.safestop.trace_backend import PgSafestopTraceBackend
from opengrid.trace import TraceStore

logger = logging.getLogger("opengrid.safestop.main")

PROCESS_NAME = "safestop"
DEFAULT_KEY_ID = "safestop-2026a"
DEFAULT_HEARTBEAT_INTERVAL_S = 5.0


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

    logger.warning("unrecognised safestop request action", extra={"action": action})


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
    mqtt_username = "og_safestop"
    mqtt_password = os.environ.get("OG_MQTT_SAFESTOP_PASSWORD", "")

    backend = PgStopEventBackend(pool)
    trace = TraceStore(PgSafestopTraceBackend(pool))
    broker = ConfirmationBroker(
        confirm_window_s=int(cfg.get("safestop.confirm_window_s", 30)),
    )

    heartbeat_interval_s = float(cfg.get("health.heartbeat_interval_s", DEFAULT_HEARTBEAT_INTERVAL_S))

    async with build_client(
        cfg, username=mqtt_username, password=mqtt_password, client_id="og-safestop"
    ) as client:
        publisher = AiomqttStopPublisher(client=client, config=cfg)
        safestop.configure_service(SafestopService(stop_key, backend, publisher, trace))

        intake_task = asyncio.create_task(_request_intake_loop(pool, broker))
        try:

            async def _tick() -> None:
                broker.discard_expired()
                await write_heartbeat(pool, PROCESS_NAME)

            await run_forever(_tick, interval_s=heartbeat_interval_s, process_name=PROCESS_NAME)
        finally:
            intake_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await intake_task
            await pool.close()
            safestop.configure_service(None)


def _entrypoint() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    _entrypoint()
