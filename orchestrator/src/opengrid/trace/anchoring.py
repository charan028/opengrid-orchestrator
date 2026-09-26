"""K11 external anchoring (00-invariants.md, adversarial review): periodically publish the trace
chain's head hash OUTSIDE the database, so `og.trace`/`og.trace_checkpoint` are never the only place the
chain's integrity can be checked from.

Reuses `TraceStore.checkpoint()` for the actual chain-head-hash computation and `og.trace_checkpoint`
persistence (K11's own checkpoint mechanism, 02a S8.3) -- this module only signs that hash (Ed25519 via
`opengrid.core.crypto`, never re-implemented), writes it to one or more configured filesystem locations,
records the publish in `og.trace_anchor` (migration 0028), and stamps `og.trace_checkpoint.anchor_ref`.
`opengrid.invariants` calls `publish_anchor` on its own 15-minute cadence and separately verifies
freshness (`invariants.checks`/`queries` "anchor" functions) -- this module never checks its own
freshness, matching K11's own "primary -> independent check" split.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from psycopg_pool import AsyncConnectionPool

from opengrid.core.crypto import sign_payload
from opengrid.platform.config import Config
from opengrid.trace.store import TraceStore

logger = logging.getLogger(__name__)

#: Relative, per-workspace defaults (matches `[pq_ingest].blob_store_dir`'s convention) -- production
#: overrides via `[trace].anchor_dir`/`anchor_secondary_dir` (e.g. `/var/lib/opengrid/anchors` plus a
#: second, Gitea-independent location, per the task brief's "minimum").
DEFAULT_ANCHOR_DIR = Path("var/anchors")
DEFAULT_SECONDARY_ANCHOR_DIR = Path("var/anchors_secondary")


@dataclass(frozen=True, slots=True)
class AnchorKey:
    key_id: str
    seed: bytes | None  # None: anchors publish unsigned (degraded, logged) -- never blocks publishing


def load_anchor_key(cfg: Config) -> AnchorKey:
    """`[trace].anchor_key_path` (mirrors `[guardian]`/`[safestop].key_path`'s convention: a raw 32-byte
    Ed25519 seed file). No key configured, or the file can't be read -> unsigned anchors (K7: degrade,
    don't trip -- a missing signing key must never stop the chain's head hash from being anchored at
    all; an unsigned anchor is still independent evidence of the head hash at that time)."""
    key_id = str(cfg.get("trace.anchor_key_id", "anchor-default"))
    key_path = cfg.get("trace.anchor_key_path")
    if not key_path:
        return AnchorKey(key_id=key_id, seed=None)
    try:
        seed = Path(str(key_path)).read_bytes()
    except OSError:
        logger.warning("trace.anchor_key_path configured but unreadable; publishing unsigned anchors")
        return AnchorKey(key_id=key_id, seed=None)
    return AnchorKey(key_id=key_id, seed=seed)


def _anchor_dirs(cfg: Config) -> tuple[Path, Path]:
    primary = Path(str(cfg.get("trace.anchor_dir", DEFAULT_ANCHOR_DIR)))
    secondary = Path(str(cfg.get("trace.anchor_secondary_dir", DEFAULT_SECONDARY_ANCHOR_DIR)))
    return primary, secondary


def _write_anchor_file(directory: Path, anchor_id: UUID, document: dict[str, object]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{anchor_id}.json"
    path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
    return path


_SELECT_LATEST_CHECKPOINT_SQL = """
    SELECT checkpoint_id FROM og.trace_checkpoint WHERE checkpoint_hash = %(checkpoint_hash)s
    ORDER BY checkpoint_at DESC LIMIT 1
"""
_UPDATE_ANCHOR_REF_SQL = (
    "UPDATE og.trace_checkpoint SET anchor_ref = %(anchor_ref)s WHERE checkpoint_id = %(checkpoint_id)s"
)
_INSERT_TRACE_ANCHOR_SQL = """
    INSERT INTO og.trace_anchor
        (anchor_id, checkpoint_id, published_at, checkpoint_hash, primary_path, secondary_path,
         key_id, signature)
    VALUES (%(anchor_id)s, %(checkpoint_id)s, %(published_at)s, %(checkpoint_hash)s, %(primary_path)s,
            %(secondary_path)s, %(key_id)s, %(signature)s)
"""


@dataclass(frozen=True, slots=True)
class AnchorResult:
    anchor_id: UUID
    checkpoint_id: UUID | None
    checkpoint_hash: str
    primary_path: str
    secondary_path: str | None
    signed: bool


async def publish_anchor(pool: AsyncConnectionPool, trace_store: TraceStore, cfg: Config) -> AnchorResult:
    """K11 external anchoring: checkpoint the chain (`TraceStore.checkpoint()`, K11's own mechanism),
    sign the resulting hash if a key is configured, write it to the primary anchor directory and a
    second, independent copy (best-effort -- a secondary-copy failure never blocks the primary one or
    the DB record), record the publish in `og.trace_anchor`, and stamp the checkpoint's `anchor_ref`
    with the primary anchor file's path.
    """
    checkpoint_hash = await trace_store.checkpoint()
    now = datetime.now(UTC)
    anchor_id = uuid4()
    key = load_anchor_key(cfg)
    document: dict[str, object] = {
        "anchor_id": str(anchor_id),
        "checkpoint_hash": checkpoint_hash,
        "published_at": now.isoformat(),
        "key_id": key.key_id,
    }
    signature: str | None = None
    if key.seed is not None:
        signature = sign_payload(key.seed, dict(document))
    document["signature"] = signature

    primary_dir, secondary_dir = _anchor_dirs(cfg)
    primary_path = _write_anchor_file(primary_dir, anchor_id, document)
    secondary_path: Path | None = None
    try:
        secondary_path = _write_anchor_file(secondary_dir, anchor_id, document)
    except OSError:
        logger.warning("trace anchor secondary copy failed; primary copy still written", exc_info=True)

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_SELECT_LATEST_CHECKPOINT_SQL, {"checkpoint_hash": checkpoint_hash})
        row = await cur.fetchone()
        checkpoint_id: UUID | None = row[0] if row is not None else None
        if checkpoint_id is not None:
            await cur.execute(
                _UPDATE_ANCHOR_REF_SQL, {"anchor_ref": str(primary_path), "checkpoint_id": checkpoint_id}
            )
        await cur.execute(
            _INSERT_TRACE_ANCHOR_SQL,
            {
                "anchor_id": anchor_id,
                "checkpoint_id": checkpoint_id,
                "published_at": now,
                "checkpoint_hash": checkpoint_hash,
                "primary_path": str(primary_path),
                "secondary_path": str(secondary_path) if secondary_path else None,
                "key_id": key.key_id,
                "signature": signature,
            },
        )
        await conn.commit()

    return AnchorResult(
        anchor_id=anchor_id,
        checkpoint_id=checkpoint_id,
        checkpoint_hash=checkpoint_hash,
        primary_path=str(primary_path),
        secondary_path=str(secondary_path) if secondary_path else None,
        signed=signature is not None,
    )
