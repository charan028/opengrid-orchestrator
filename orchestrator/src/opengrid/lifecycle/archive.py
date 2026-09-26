"""Hot -> cold tier: day exports to `<cold_root>/<archive>/<yyyy>/<mm>/<dd>.csv.<zst|gz>` with a manifest,
checksum verification before any drop, the monthly write-once billing export and the audit restore.

Files are write-once: written to a `.partial` name, fsynced, renamed, then made read-only (0440). A
re-export of a day that gained late rows gets a new revision file (`<dd>.r2.csv.zst`); the registry
(`og.lifecycle_archive`) points at the latest. The SHA-256 covers the compressed file bytes and is
re-derived from disk (`verify_archive`) before the job drops anything.

Codec: zstd when the `zstandard` package is importable, else gzip from the standard library (so the job
runs today without a new dependency; both are recorded in the manifest and handled by `restore`).
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from psycopg import IsolationLevel, sql
from psycopg_pool import AsyncConnectionPool

from opengrid.lifecycle.policy import ArchiveSpec, LifecycleConfig, archive_spec, day_start, month_bounds

logger = logging.getLogger(__name__)

READ_ONLY_MODE = 0o440
DIR_MODE = 0o750
_CHUNK = 1 << 20
_ONE_DAY = timedelta(days=1)

try:  # optional: pinned BSD package, preferred codec when present
    import zstandard as _zstd  # type: ignore[import-not-found, unused-ignore]
except ImportError:  # pragma: no cover - depends on the host environment
    _zstd = None


class ArchiveError(RuntimeError):
    """An export or a verification failed: the caller must not drop the data."""


def codec_name() -> str:
    return "zstd" if _zstd is not None else "gzip"


def _extension(codec: str) -> str:
    return "zst" if codec == "zstd" else "gz"


@dataclass(frozen=True, slots=True)
class Manifest:
    archive_name: str
    day: str
    path: str
    codec: str
    format: str
    row_count: int
    min_ts: str | None
    max_ts: str | None
    sha256: str
    bytes: int
    revision: int
    created_at: str


class _Sink(Protocol):
    def write(self, data: bytes, /) -> int: ...


class _HashingFile:
    """File-like wrapper: every byte written to disk also feeds the SHA-256 and the byte count."""

    def __init__(self, fh: BinaryIO) -> None:
        self._fh = fh
        self.sha = hashlib.sha256()
        self.size = 0

    def write(self, data: bytes, /) -> int:
        self.sha.update(data)
        self.size += len(data)
        return self._fh.write(data)

    def flush(self) -> None:
        self._fh.flush()


class ArchiveWriter:
    """Synchronous compressed writer for one archive file (the async COPY loop feeds it chunks)."""

    def __init__(self, final_path: Path, codec: str, zstd_level: int = 6) -> None:
        self.final_path = final_path
        self.partial_path = final_path.with_name(final_path.name + ".partial")
        final_path.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        self._raw: BinaryIO = self.partial_path.open("wb")
        self._hashing = _HashingFile(self._raw)
        self._gzip: gzip.GzipFile | None = None
        self._zstd_writer: Any = None
        if codec == "zstd" and _zstd is not None:
            self._zstd_writer = _zstd.ZstdCompressor(level=zstd_level).stream_writer(
                self._hashing, closefd=False
            )
        else:
            self._gzip = gzip.GzipFile(fileobj=self._hashing, mode="wb", compresslevel=6, mtime=0)

    def write(self, data: bytes) -> None:
        if self._zstd_writer is not None:
            self._zstd_writer.write(data)
        elif self._gzip is not None:
            self._gzip.write(data)

    def finish(self) -> tuple[str, int]:
        """Close the stream, fsync, rename into place read-only. Returns (sha256 hex, bytes)."""
        if self._zstd_writer is not None:
            self._zstd_writer.close()
        elif self._gzip is not None:
            self._gzip.close()
        self._raw.flush()
        os.fsync(self._raw.fileno())
        self._raw.close()
        self.partial_path.replace(self.final_path)
        self.final_path.chmod(READ_ONLY_MODE)
        return self._hashing.sha.hexdigest(), self._hashing.size

    def abort(self) -> None:
        try:
            with contextlib.suppress(Exception):
                if self._zstd_writer is not None:
                    self._zstd_writer.close()
                elif self._gzip is not None:
                    self._gzip.close()
            self._raw.close()
        finally:
            self.partial_path.unlink(missing_ok=True)


def file_sha256(path: Path) -> tuple[str, int]:
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            sha.update(chunk)
            size += len(chunk)
    return sha.hexdigest(), size


def write_manifest(path: Path, manifest: Manifest) -> None:
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(asdict(manifest), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    partial.replace(path)
    path.chmod(READ_ONLY_MODE)


def archive_paths(
    cold_root: Path, archive_name: str, day: date, codec: str, revision: int
) -> tuple[Path, Path]:
    """(data file, manifest file) for one day. Revision 1 is `<dd>.csv.<ext>`, later `<dd>.r<n>.csv.<ext>`."""
    folder = cold_root / archive_name / f"{day:%Y}" / f"{day:%m}"
    stem = f"{day:%d}" if revision == 1 else f"{day:%d}.r{revision}"
    return folder / f"{stem}.csv.{_extension(codec)}", folder / f"{stem}.manifest.json"


def _decompressing_reader(path: Path, codec: str) -> Any:
    raw = path.open("rb")
    if codec == "zstd":
        if _zstd is None:
            raw.close()
            raise ArchiveError(f"{path}: zstd archive but the zstandard package is not installed")
        return _zstd.ZstdDecompressor().stream_reader(raw, closefd=True)
    return gzip.GzipFile(fileobj=raw, mode="rb")


def cold_free_gb(cold_root: Path) -> float:
    import shutil

    probe = cold_root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free / 1e9


# ---------------------------------------------------------------------------------------------------
# Registry (og.lifecycle_archive)
# ---------------------------------------------------------------------------------------------------

_REGISTRY_GET_SQL = """
SELECT path, codec, row_count, sha256, bytes, revision
FROM og.lifecycle_archive WHERE archive_name = %(name)s AND day = %(day)s
"""

_REGISTRY_UPSERT_SQL = """
INSERT INTO og.lifecycle_archive (archive_name, day, path, manifest_path, codec, row_count, min_ts, max_ts,
                                  sha256, bytes, revision, verified_at)
VALUES (%(archive_name)s, %(day)s, %(path)s, %(manifest_path)s, %(codec)s, %(row_count)s, %(min_ts)s,
        %(max_ts)s, %(sha256)s, %(bytes)s, %(revision)s, now())
ON CONFLICT (archive_name, day) DO UPDATE SET
    path = EXCLUDED.path, manifest_path = EXCLUDED.manifest_path, codec = EXCLUDED.codec,
    row_count = EXCLUDED.row_count, min_ts = EXCLUDED.min_ts, max_ts = EXCLUDED.max_ts,
    sha256 = EXCLUDED.sha256, bytes = EXCLUDED.bytes, revision = EXCLUDED.revision,
    created_at = now(), verified_at = now()
"""

_REGISTRY_VERIFIED_SQL = (
    "UPDATE og.lifecycle_archive SET verified_at = now() WHERE archive_name = %(name)s AND day = %(day)s"
)


@dataclass(frozen=True, slots=True)
class ArchivedDay:
    path: Path
    codec: str
    row_count: int
    sha256: str
    bytes: int
    revision: int


async def registry_get(pool: AsyncConnectionPool, name: str, day: date) -> ArchivedDay | None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_REGISTRY_GET_SQL, {"name": name, "day": day})
        row = await cur.fetchone()
    if row is None:
        return None
    return ArchivedDay(Path(row[0]), row[1], int(row[2]), row[3], int(row[4]), int(row[5]))


async def registry_days(pool: AsyncConnectionPool, name: str) -> dict[date, int]:
    """{day: row_count} of every registered export of `name`."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT day, row_count FROM og.lifecycle_archive WHERE archive_name = %(name)s", {"name": name}
        )
        rows = await cur.fetchall()
    return {r[0]: int(r[1]) for r in rows}


# ---------------------------------------------------------------------------------------------------
# Export / verify
# ---------------------------------------------------------------------------------------------------


async def export_range(
    pool: AsyncConnectionPool,
    cfg: LifecycleConfig,
    spec: ArchiveSpec,
    *,
    day: date,
    lo: datetime,
    hi: datetime,
    source: str | None = None,
    data_path: Path | None = None,
    manifest_path: Path | None = None,
) -> Manifest:
    """Export rows of `spec` in [lo, hi) -- from `source` (a detached partition name) when given -- to a
    new write-once file, register it and return its manifest. Count, min/max and the COPY run in one
    REPEATABLE READ snapshot, so the manifest describes exactly the bytes written."""
    if cold_free_gb(cfg.cold_root) < cfg.cold_min_free_gb:
        raise ArchiveError(f"cold tier {cfg.cold_root} has < {cfg.cold_min_free_gb} GB free; not exporting")
    codec = codec_name()
    previous = await registry_get(pool, spec.name, day)
    revision = 1 if previous is None else previous.revision + 1
    if data_path is None or manifest_path is None:
        data_path, manifest_path = archive_paths(cfg.cold_root, spec.name, day, codec, revision)
    if data_path.exists():
        raise ArchiveError(f"{data_path} already exists (write-once); refusing to overwrite")

    select_sql = spec.select_sql
    ts_sql = spec.ts_sql
    if source is not None:
        # A detached partition keeps the parent's shape: read it instead of the parent.
        select_sql = select_sql.replace(f'og."{spec.like_table}"', f'og."{source}"')
        ts_sql = ts_sql.replace(f'og."{spec.like_table}"', f'og."{source}"')
    params = {"lo": lo, "hi": hi}
    writer = ArchiveWriter(data_path, codec, cfg.zstd_level)
    try:
        async with pool.connection() as conn:
            await conn.set_isolation_level(IsolationLevel.REPEATABLE_READ)
            try:
                async with conn.transaction(), conn.cursor() as cur:
                    await cur.execute(ts_sql, params)
                    stats = await cur.fetchone()
                    if stats is None:
                        raise ArchiveError(f"{spec.name} {day}: no statistics row")
                    copy_sql = sql.SQL("COPY ({}) TO STDOUT WITH (FORMAT csv, HEADER true)").format(
                        sql.SQL(select_sql)
                    )
                    async with cur.copy(copy_sql, params) as copy:
                        async for chunk in copy:
                            writer.write(bytes(chunk))
            finally:
                await conn.set_isolation_level(None)
        sha, size = writer.finish()
    except Exception as exc:
        writer.abort()
        raise ArchiveError(f"export of {spec.name} {day} failed: {exc}") from exc

    row_count, min_ts, max_ts = int(stats[0]), stats[1], stats[2]
    manifest = Manifest(
        archive_name=spec.name,
        day=day.isoformat(),
        path=str(data_path),
        codec=codec,
        format="csv+header",
        row_count=row_count,
        min_ts=min_ts.isoformat() if min_ts else None,
        max_ts=max_ts.isoformat() if max_ts else None,
        sha256=sha,
        bytes=size,
        revision=revision,
        created_at=datetime.now(UTC).isoformat(),
    )
    write_manifest(manifest_path, manifest)
    verify_file(data_path, sha, size)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            _REGISTRY_UPSERT_SQL,
            {
                "archive_name": spec.name,
                "day": day,
                "path": str(data_path),
                "manifest_path": str(manifest_path),
                "codec": codec,
                "row_count": row_count,
                "min_ts": min_ts,
                "max_ts": max_ts,
                "sha256": sha,
                "bytes": size,
                "revision": revision,
            },
        )
    logger.info(
        "lifecycle archive written",
        extra={
            "archive": spec.name,
            "day": day.isoformat(),
            "rows": row_count,
            "bytes": size,
            "path": str(data_path),
        },
    )
    return manifest


def verify_file(path: Path, sha256: str, size: int) -> None:
    """Re-read the file from disk and compare its SHA-256 and size with the manifest's."""
    if not path.exists():
        raise ArchiveError(f"{path}: archive file missing")
    actual_sha, actual_size = file_sha256(path)
    if actual_sha != sha256 or actual_size != size:
        raise ArchiveError(
            f"{path}: checksum mismatch (have {actual_sha}/{actual_size}, want {sha256}/{size})"
        )


async def verify_archive(pool: AsyncConnectionPool, name: str, day: date) -> ArchivedDay:
    """Verify the registered export of (name, day) against its file; raises ArchiveError."""
    archived = await registry_get(pool, name, day)
    if archived is None:
        raise ArchiveError(f"{name} {day}: no archive registered")
    verify_file(archived.path, archived.sha256, archived.bytes)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_REGISTRY_VERIFIED_SQL, {"name": name, "day": day})
    return archived


async def ensure_day_archived(
    pool: AsyncConnectionPool,
    cfg: LifecycleConfig,
    spec: ArchiveSpec,
    day: date,
    *,
    expected_rows: int,
    source: str | None = None,
) -> ArchivedDay | None:
    """Make sure an export of `day` exists, holds `expected_rows` rows and verifies. Re-exports (new
    revision) when the registered export has a different row count (late rows). Returns None for a day
    with no rows (nothing to archive)."""
    archived = await registry_get(pool, spec.name, day)
    if archived is None or archived.row_count != expected_rows:
        if expected_rows == 0 and archived is None:
            return None
        lo = day_start(day)
        await export_range(pool, cfg, spec, day=day, lo=lo, hi=lo + _ONE_DAY, source=source)
    return await verify_archive(pool, spec.name, day)


# ---------------------------------------------------------------------------------------------------
# Monthly billing export (write-once, no delete)
# ---------------------------------------------------------------------------------------------------


async def export_billing_month(
    pool: AsyncConnectionPool, cfg: LifecycleConfig, month: date
) -> dict[str, Any]:
    """Write `<cold_root>/billing/<yyyy>/<mm>/{invoice_line,pnl}.csv.<ext>` plus a combined manifest whose
    `digest` is the SHA-256 over the member files' hashes ("signed by checksum"). Never re-written: a
    month already registered is skipped. Rows are selected by `created_at` (lines are insert-only and
    versioned, so a closed month never changes; corrections land in a later month)."""
    from opengrid.lifecycle.policy import BILLING_ARCHIVES

    folder = cfg.cold_root / "billing" / f"{month:%Y}" / f"{month:%m}"
    combined = folder / "manifest.json"
    if combined.exists():
        return {"month": month.isoformat(), "skipped": "exists"}
    lo, hi = month_bounds(month)
    codec = codec_name()
    members: list[Manifest] = []
    for spec in BILLING_ARCHIVES:
        if await registry_get(pool, spec.name, month) is not None:
            return {"month": month.isoformat(), "skipped": "registered"}
        short = spec.like_table
        data_path = folder / f"{short}.csv.{_extension(codec)}"
        manifest_path = folder / f"{short}.manifest.json"
        members.append(
            await export_range(
                pool, cfg, spec, day=month, lo=lo, hi=hi, data_path=data_path, manifest_path=manifest_path
            )
        )
    digest = hashlib.sha256("".join(m.sha256 for m in members).encode("ascii")).hexdigest()
    payload = {
        "month": month.isoformat(),
        "files": [asdict(m) for m in members],
        "digest": digest,
        "created_at": datetime.now(UTC).isoformat(),
    }
    combined.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    combined.chmod(READ_ONLY_MODE)
    return {
        "month": month.isoformat(),
        "digest": digest,
        "rows": {m.archive_name: m.row_count for m in members},
    }


# ---------------------------------------------------------------------------------------------------
# Restore (audits): load one archived day into a scratch table
# ---------------------------------------------------------------------------------------------------

RESTORE_SCHEMA = "og_restore"


async def restore_day(
    pool: AsyncConnectionPool, name: str, day: date, *, schema: str = RESTORE_SCHEMA
) -> dict[str, Any]:
    """Verify the archive of (name, day), then load it into `<schema>.<name>_<yyyymmdd>` (dropped and
    recreated, `LIKE og.<table>`). Returns the target and the row count; raises ArchiveError when the
    checksum or the row count does not match the manifest."""
    spec = archive_spec(name)
    archived = await verify_archive(pool, name, day)
    target = f"{name}_{day:%Y%m%d}"
    reader = _decompressing_reader(archived.path, archived.codec)
    try:
        header = _read_header(reader)
        columns = sql.SQL(", ").join(sql.Identifier(c) for c in header)
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
            await cur.execute(
                sql.SQL("DROP TABLE IF EXISTS {}.{}").format(sql.Identifier(schema), sql.Identifier(target))
            )
            await cur.execute(
                sql.SQL("CREATE TABLE {}.{} (LIKE og.{} INCLUDING DEFAULTS)").format(
                    sql.Identifier(schema), sql.Identifier(target), sql.Identifier(spec.like_table)
                )
            )
            copy_sql = sql.SQL("COPY {}.{} ({}) FROM STDIN WITH (FORMAT csv)").format(
                sql.Identifier(schema), sql.Identifier(target), columns
            )
            async with cur.copy(copy_sql) as copy:
                while chunk := reader.read(_CHUNK):
                    await copy.write(chunk)
            await cur.execute(
                sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(target))
            )
            count_row = await cur.fetchone()
    finally:
        reader.close()
    loaded = int(count_row[0]) if count_row else 0
    if loaded != archived.row_count:
        raise ArchiveError(f"{name} {day}: restored {loaded} rows, manifest says {archived.row_count}")
    return {
        "table": f"{schema}.{target}",
        "rows": loaded,
        "sha256": archived.sha256,
        "path": str(archived.path),
    }


def _read_header(reader: Any) -> list[str]:
    """Consume the CSV header line (column names never contain newlines or quotes here)."""
    buf = bytearray()
    while True:
        b = reader.read(1)
        if not b or b == b"\n":
            break
        buf += b
    return [c.strip().strip('"') for c in buf.decode("utf-8").rstrip("\r").split(",")]
