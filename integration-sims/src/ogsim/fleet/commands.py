"""ogsim.fleet.commands -- command_batch verification (02b §4.3, K6).

Pure logic, no MQTT: given a received `command_batch.schema.json` object,
the guardian's public key, and each hub's last accepted (epoch, seq),
verifies signature then freshness, per crypto.md's "freshness checks
happen after signature verification and are independent of it."
Returns one `CommandVerdict` per item so the caller can ack each hub.

`build_ack` builds the corresponding `interfaces/mqtt/ack.schema.json`
message for one verdict (fleet -> orchestrator, `<root>/ack/<hub_id>`,
QoS 1) -- also pure, so the ack shape is unit-testable without MQTT.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ogsim.common.crypto import verify_signature
from ogsim.common.scenario import utc_timestamp

RejectReason = str  # one of BAD_SIGNATURE | STALE_EPOCH | STALE_SEQ | EXPIRED

_SIGNED_FIELDS = ("batch_id", "bank_id", "epoch", "seq", "issued_at", "expires_at", "items")


@dataclass(frozen=True)
class CommandVerdict:
    hub_id: str
    accepted: bool
    reject_reason: RejectReason | None
    requested_p_kw_setpoint: float | None


def parse_rfc3339(value: str) -> datetime:
    """Parses an RFC3339 UTC timestamp such as `issued_at`/`expires_at`."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _signing_fields(batch: dict[str, Any]) -> dict[str, Any]:
    return {k: batch[k] for k in _SIGNED_FIELDS if k in batch}


def verify_batch_signature(batch: dict[str, Any], public_key: Ed25519PublicKey) -> bool:
    """Verifies the batch envelope's Ed25519 signature (crypto.md §2.1)."""
    signature = batch.get("signature")
    return verify_signature(public_key, _signing_fields(batch), signature)


def _freshness_reject(
    batch_epoch: int, batch_seq: int, last_epoch: int, last_seq: int
) -> RejectReason | None:
    if batch_epoch < last_epoch:
        return "STALE_EPOCH"
    if batch_epoch == last_epoch and batch_seq <= last_seq:
        return "STALE_SEQ"
    return None


def evaluate_batch(
    batch: dict[str, Any],
    public_key: Ed25519PublicKey,
    last_accepted: dict[str, tuple[int, int]],
    now: datetime,
) -> list[CommandVerdict]:
    """Evaluates every item in `batch` against signature and freshness
    rules. `last_accepted` maps hub_id -> (last_epoch, last_seq), -1/-1 if
    never accepted. Does not mutate `last_accepted`; the caller applies
    accepted verdicts and updates state itself.

    `precondition.ledger_version`, if present, is accepted unconditionally:
    the sim has no ledger to check it against (BUILD.md instructs
    structural pass-through only).
    """
    items: list[dict[str, Any]] = batch.get("items", [])
    if not verify_batch_signature(batch, public_key):
        return [CommandVerdict(item["hub_id"], False, "BAD_SIGNATURE", None) for item in items]

    epoch = int(batch["epoch"])
    seq = int(batch["seq"])
    issued_at = parse_rfc3339(batch["issued_at"])
    expires_at = parse_rfc3339(batch["expires_at"])
    expired = not (issued_at <= now < expires_at)

    verdicts: list[CommandVerdict] = []
    for item in items:
        hub_id = item["hub_id"]
        last_epoch, last_seq = last_accepted.get(hub_id, (-1, -1))
        reason = _freshness_reject(epoch, seq, last_epoch, last_seq)
        if reason is None and expired:
            reason = "EXPIRED"
        accepted = reason is None
        setpoint = float(item["p_kw_setpoint"]) if accepted else None
        verdicts.append(CommandVerdict(hub_id, accepted, reason, setpoint))
    return verdicts


def utc_now_from_epoch(epoch_seconds: float) -> datetime:
    """Converts a Unix-epoch float (from `Clock.now()`) to an aware UTC datetime."""
    return datetime.fromtimestamp(epoch_seconds, tz=UTC)


def build_ack(
    verdict: CommandVerdict, batch_id: str, applied_p_kw: float | None, now_epoch: float
) -> dict[str, Any]:
    """Builds one `ack.schema.json`-conformant ack for `verdict`.

    `applied_p_kw` is the hub's actual post-physics applied power (after the
    SoC/reserve clamp, see `ogsim.fleet.physics.clip_commanded_setpoint`),
    supplied by the caller since a `CommandVerdict` alone carries only the
    *requested* setpoint. Always reported as `None` for a rejected verdict,
    regardless of what the caller passes in, since a rejected command was
    never applied.
    """
    return {
        "hub_id": verdict.hub_id,
        "batch_id": batch_id,
        "accepted": verdict.accepted,
        "applied_p_kw": applied_p_kw if verdict.accepted else None,
        "reject_reason": verdict.reject_reason,
        "ts": utc_timestamp(now_epoch),
    }
