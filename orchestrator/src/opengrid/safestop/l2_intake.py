"""Utility L2 BLOCK/ESTOP intake for `og-safestop` (K5, K8).

The utility SCADA gateway publishes `ScadaUtilityInstruction`s on `<root>/scada/instruction/<bank_id>`
(QoS 1, possibly retained). The guardian enforces LIMIT; a BLOCK or ESTOP is a hard stop, so
og-safestop also acts on it *itself*, without og-engine or og-guardian: it engages a BANK-scope stop
on that bank with the stop-only key. LIMIT and expired instructions are ignored here.

Idempotency: one instruction id engages at most once. An in-memory `handled` set absorbs QoS 1
duplicates and retained re-delivery on reconnect; `already_acted_fn` (backed by
`PgStopEventBackend.has_l2_engage`, which looks for the instruction id in a UTILITY ENGAGE row's
`reason` for that bank) makes it survive a restart. A *new* instruction id for a bank that is already
stopped still engages: stops are per `stop_id`, each needs its own guardian Tier-2 RELEASE, and a second
utility instruction must not be silently absorbed by an earlier stop that might be released first.

`handle_instruction` is pure apart from the two injected callables, so it is unit-tested without MQTT
or Postgres; `build_l2_session` is the thin MQTT session `main.py` runs as a background task.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from opengrid.core.models.mqtt import ScadaUtilityInstruction
from opengrid.platform.config import Config
from opengrid.platform.mqtt import build_client, topic, validate_payload
from opengrid.platform.mqtt_session import MqttSession
from opengrid.safestop.events import is_safe_scope_ref

logger = logging.getLogger(__name__)

#: MQTT process name (client id `og-safestop-l2` in production): a second connection, separate from the
#: daemon's publishing client `og-safestop`, so this subscriber can reconnect on its own.
L2_PROCESS_NAME = "safestop-l2"
INSTRUCTION_TOPIC_SUFFIX = "scada/instruction/+"
DEFAULT_RECONNECT_DELAY_S = 5.0

#: (bank_id, reason, initiator_ref) -> engage a BANK stop with initiator_kind UTILITY.
EngageFn = Callable[[str, str, str], Awaitable[object]]
#: (instruction_id, bank_id) -> was an ENGAGE for this instruction already recorded?
AlreadyActedFn = Callable[[UUID, str], Awaitable[bool]]
#: (instruction_id, bank_id) -> queue the recorded ENGAGE's publication if it has none (H5 repair).
EnsurePublishedFn = Callable[[UUID, str], Awaitable[object]]

L2Outcome = Literal[
    "ENGAGED",
    "IGNORED_LIMIT",
    "IGNORED_EXPIRED",
    "DUPLICATE",
    "ALREADY_ACTED",
    "MALFORMED",
    "FAILED",
]

_STOP_KINDS = frozenset({"BLOCK", "ESTOP"})


def l2_reason(instruction: ScadaUtilityInstruction) -> str:
    """The stop reason (also the `og.stop_event.reason` / trace `reason`): carries the instruction id,
    which is what `PgStopEventBackend.has_l2_engage` matches on."""
    return f"L2 {instruction.kind} {instruction.instruction_id} from {instruction.issued_by}"


def l2_initiator_ref(instruction: ScadaUtilityInstruction) -> str:
    return f"utility:{instruction.issued_by}"


def parse_instruction(
    message: bytes | bytearray | dict[str, Any] | ScadaUtilityInstruction,
) -> ScadaUtilityInstruction:
    """Raw MQTT payload (or an already-decoded dict/model) -> validated instruction. Raises on anything
    malformed (JSON, schema, or model)."""
    if isinstance(message, ScadaUtilityInstruction):
        return message
    data = json.loads(message) if isinstance(message, bytes | bytearray) else message
    if not isinstance(data, dict):
        raise ValueError("instruction payload is not a JSON object")
    validate_payload("scada_utility_instruction", data)
    return ScadaUtilityInstruction.model_validate(data)


async def handle_instruction(
    message: bytes | bytearray | dict[str, Any] | ScadaUtilityInstruction,
    *,
    now: datetime,
    handled: set[UUID],
    engage_fn: EngageFn,
    already_acted_fn: AlreadyActedFn,
    topic_bank_id: str | None = None,
    ensure_published_fn: EnsurePublishedFn | None = None,
) -> L2Outcome:
    """Act on one utility instruction. Never raises (except cancellation): a malformed message or a
    failed engage is logged and reported as an outcome, so one bad message never kills the listener.

    `topic_bank_id` is the `<bank_id>` topic level the message arrived on; a payload naming a different
    bank is refused (a sender permitted on one bank's topic must not stop another bank).

    `ensure_published_fn` (H5): on an instruction already acted on, make sure its recorded ENGAGE is also
    queued to publish -- a stop recorded without a publication would otherwise never reach the hubs."""
    try:
        instruction = parse_instruction(message)
    except Exception as exc:
        logger.warning("malformed utility L2 instruction ignored", extra={"error": str(exc)})
        return "MALFORMED"

    bank_id = instruction.bank_id
    if not is_safe_scope_ref(bank_id) or (topic_bank_id is not None and topic_bank_id != bank_id):
        logger.warning(
            "utility L2 instruction with unusable or mismatched bank_id ignored",
            extra={
                "instruction_id": str(instruction.instruction_id),
                "bank_id": bank_id,
                "topic_bank_id": topic_bank_id,
            },
        )
        return "MALFORMED"

    if instruction.kind not in _STOP_KINDS:
        return "IGNORED_LIMIT"  # LIMIT is the guardian's to enforce, not a stop
    expires_at = instruction.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)  # a naive wire timestamp is taken as UTC
    if expires_at is not None and expires_at <= now:
        logger.info(
            "expired utility L2 instruction ignored",
            extra={"instruction_id": str(instruction.instruction_id), "bank_id": bank_id},
        )
        return "IGNORED_EXPIRED"
    if instruction.instruction_id in handled:
        return "DUPLICATE"

    try:
        if await already_acted_fn(instruction.instruction_id, bank_id):
            if ensure_published_fn is not None:
                await ensure_published_fn(instruction.instruction_id, bank_id)
            handled.add(instruction.instruction_id)
            logger.info(
                "utility L2 instruction already acted on; not engaging again",
                extra={"instruction_id": str(instruction.instruction_id), "bank_id": bank_id},
            )
            return "ALREADY_ACTED"
        await engage_fn(bank_id, l2_reason(instruction), l2_initiator_ref(instruction))
    except Exception:
        # Not marked handled: a re-delivery (reconnect, retained) retries it.
        logger.exception(
            "safe stop for utility L2 instruction FAILED",
            extra={"instruction_id": str(instruction.instruction_id), "bank_id": bank_id},
        )
        return "FAILED"

    handled.add(instruction.instruction_id)
    logger.warning(
        "bank safe stop engaged on utility L2 instruction",
        extra={
            "instruction_id": str(instruction.instruction_id),
            "bank_id": bank_id,
            "kind": instruction.kind,
            "issued_by": instruction.issued_by,
        },
    )
    return "ENGAGED"


def build_l2_session(
    cfg: Config,
    *,
    username: str,
    password: str,
    engage_fn: EngageFn,
    already_acted_fn: AlreadyActedFn,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    min_backoff_s: float = DEFAULT_RECONNECT_DELAY_S,
    ensure_published_fn: EnsurePublishedFn | None = None,
) -> MqttSession:
    """The utility L2 instruction listener: `<root>/scada/instruction/+` (QoS 1) on its own connection
    (`L2_PROCESS_NAME`), kept up across broker disconnects by `MqttSession` (backoff from `min_backoff_s`,
    the same client id, never two clients at once; `og_mqtt_reconnects_total{client="safestop-l2"}`).
    Parsing follows `opengrid.guardian.mqtt_io`'s input handler (JSON -> schema -> model), without importing
    it. `main.py` runs it and fails closed while it is down."""
    handled: set[UUID] = set()
    instruction_prefix = topic(cfg, "scada/instruction/")

    async def on_message(message: Any) -> None:
        payload = message.payload
        if not isinstance(payload, bytes | bytearray) or not payload:
            return  # empty payload = a retained clear, not an instruction
        msg_topic = str(message.topic)
        topic_bank_id = (
            msg_topic[len(instruction_prefix) :] if msg_topic.startswith(instruction_prefix) else None
        )
        await handle_instruction(
            bytes(payload),
            now=clock(),
            handled=handled,
            engage_fn=engage_fn,
            already_acted_fn=already_acted_fn,
            topic_bank_id=topic_bank_id,
            ensure_published_fn=ensure_published_fn,
        )

    return MqttSession(
        lambda: build_client(cfg, username=username, password=password, process=L2_PROCESS_NAME),
        name=L2_PROCESS_NAME,
        subscriptions=[(topic(cfg, INSTRUCTION_TOPIC_SUFFIX), 1)],
        on_message=on_message,
        min_backoff_s=min_backoff_s,
    )
