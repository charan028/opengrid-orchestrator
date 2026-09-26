"""Pure lifecycle policy: configuration, the table whitelist and the day arithmetic (no I/O).

`og.data_retention` (migration 0033) holds the operator-tunable numbers (hot_days, keep_days, archive,
legal_hold). The *shape* of every table the job may touch -- its time column, how a day is exported and
how rows are deleted -- lives here as a fixed whitelist: no SQL identifier is ever read from the database,
and a table that is not in `DELETABLE_TABLES` can never be deleted from, whatever its policy row says.

Days are UTC days. A day `d` is:
  * export-eligible once `d <= today - max(hot_days, 1)` (closed, and at least `hot_days` old);
  * drop-eligible once its end is at or before `today_start - keep_days` -- so between `keep_days` and
    `keep_days + 1` days of history are always kept.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal

Mode = Literal["PARTITION", "DELETE", "NONE"]

DEFAULT_COLD_ROOT = "/srv/ogbackup/cold"
DEFAULT_PGDATA_PATH = "/srv/pgdata"


@dataclass(frozen=True, slots=True)
class LifecycleConfig:
    """`[lifecycle]` in orchestrator.toml (every key optional)."""

    cold_root: Path = Path(DEFAULT_COLD_ROOT)
    days_ahead: int = 7
    days_back: int = 1
    batch_rows: int = 10_000
    lock_timeout_s: int = 5
    rollup_backfill_hours: int = 24
    rollup_late_minutes: int = 15
    rollup_chunk_minutes: int = 15
    rollup_max_hours_per_run: int = 6
    max_delete_batches_per_table: int = 2_000
    pgdata_path: Path = Path(DEFAULT_PGDATA_PATH)
    disk_warn_pct: float = 80.0
    disk_crit_pct: float = 90.0
    cold_min_free_gb: float = 5.0
    pq_blob_dir: Path | None = None
    zstd_level: int = 6
    #: Hot->cold pre-exports per table per run (each is a full read of one day): bounds the job's I/O on a
    #: busy disk. Exports required by a drop are not capped (a drop never happens without its export).
    max_preexport_days_per_run: int = 1

    @classmethod
    def from_config(cls, cfg: Any) -> LifecycleConfig:
        """`cfg` is anything with `.get(dotted, default)` (opengrid.platform.config.Config)."""

        def get(key: str, default: Any) -> Any:
            return cfg.get(f"lifecycle.{key}", default)

        blob_dir = cfg.get("pq_ingest.blob_store_dir", None)
        base = cls()
        return cls(
            cold_root=Path(str(get("cold_root", base.cold_root))),
            days_ahead=int(get("days_ahead", base.days_ahead)),
            days_back=int(get("days_back", base.days_back)),
            batch_rows=min(int(get("batch_rows", base.batch_rows)), 10_000),
            lock_timeout_s=int(get("lock_timeout_s", base.lock_timeout_s)),
            rollup_backfill_hours=int(get("rollup_backfill_hours", base.rollup_backfill_hours)),
            rollup_late_minutes=int(get("rollup_late_minutes", base.rollup_late_minutes)),
            rollup_chunk_minutes=int(get("rollup_chunk_minutes", base.rollup_chunk_minutes)),
            rollup_max_hours_per_run=int(get("rollup_max_hours_per_run", base.rollup_max_hours_per_run)),
            max_delete_batches_per_table=int(
                get("max_delete_batches_per_table", base.max_delete_batches_per_table)
            ),
            pgdata_path=Path(str(get("pgdata_path", base.pgdata_path))),
            disk_warn_pct=float(get("disk_warn_pct", base.disk_warn_pct)),
            disk_crit_pct=float(get("disk_crit_pct", base.disk_crit_pct)),
            cold_min_free_gb=float(get("cold_min_free_gb", base.cold_min_free_gb)),
            pq_blob_dir=Path(str(get("pq_blob_dir", blob_dir))) if get("pq_blob_dir", blob_dir) else None,
            zstd_level=int(get("zstd_level", base.zstd_level)),
            max_preexport_days_per_run=int(
                get("max_preexport_days_per_run", base.max_preexport_days_per_run)
            ),
        )


@dataclass(frozen=True, slots=True)
class RetentionRow:
    """One `og.data_retention` row."""

    table_name: str
    ts_column: str
    mode: Mode
    hot_days: int | None
    keep_days: int | None
    archive: bool
    legal_hold: bool
    protected: bool


@dataclass(frozen=True, slots=True)
class ArchiveSpec:
    """How one day of a table is exported. `select_sql` is a fixed literal with `%(lo)s`/`%(hi)s`
    placeholders; `like_table` is the table a restore recreates the shape of."""

    name: str
    like_table: str
    select_sql: str
    ts_sql: str


@dataclass(frozen=True, slots=True)
class TableSpec:
    table: str
    ts_column: str
    archives: tuple[ArchiveSpec, ...]
    delete_guard: str = ""
    blob_ref_column: str | None = None
    child_verdicts: bool = False


def _plain(table: str, ts_column: str) -> ArchiveSpec:
    return ArchiveSpec(
        name=table,
        like_table=table,
        select_sql=f'SELECT * FROM og."{table}" WHERE {ts_column} >= %(lo)s AND {ts_column} < %(hi)s',  # noqa: S608 -- whitelist literals
        ts_sql=f'SELECT count(*), min({ts_column}), max({ts_column}) FROM og."{table}" '  # noqa: S608
        f"WHERE {ts_column} >= %(lo)s AND {ts_column} < %(hi)s",
    )


_VERDICT_ARCHIVE = ArchiveSpec(
    name="verdict",
    like_table="verdict",
    select_sql=(
        "SELECT v.* FROM og.verdict v JOIN og.command_batch cb USING (command_batch_id) "
        "WHERE cb.created_at >= %(lo)s AND cb.created_at < %(hi)s"
    ),
    ts_sql=(
        "SELECT count(*), min(cb.created_at), max(cb.created_at) FROM og.verdict v "
        "JOIN og.command_batch cb USING (command_batch_id) "
        "WHERE cb.created_at >= %(lo)s AND cb.created_at < %(hi)s"
    ),
)

#: Tables the lifecycle job may delete from (PARTITION or DELETE mode). Anything else -- in particular
#: invoice_line, pnl, meter_interval, performance and trace -- is never deleted, whatever its policy says.
DELETABLE_TABLES: dict[str, TableSpec] = {
    "telemetry": TableSpec("telemetry", "ts", (_plain("telemetry", "ts"),)),
    "telemetry_1m": TableSpec("telemetry_1m", "bucket", (_plain("telemetry_1m", "bucket"),)),
    "telemetry_15m": TableSpec("telemetry_15m", "bucket", (_plain("telemetry_15m", "bucket"),)),
    "command_batch": TableSpec(
        "command_batch",
        "created_at",
        (_plain("command_batch", "created_at"), _VERDICT_ARCHIVE),
        # A batch still referenced by a calibration attempt (asset-health evidence) is kept.
        delete_guard=(
            "NOT EXISTS (SELECT 1 FROM og.calibration_attempt ca "
            "WHERE ca.command_batch_id = t.command_batch_id)"
        ),
        child_verdicts=True,
    ),
    "command_ack": TableSpec("command_ack", "ts", (_plain("command_ack", "ts"),)),
    "grant": TableSpec("grant", "created_at", (_plain("grant", "created_at"),)),
    "pq_waveform_summary": TableSpec("pq_waveform_summary", "ts", (_plain("pq_waveform_summary", "ts"),)),
    "pq_waveform_raw_index": TableSpec(
        "pq_waveform_raw_index",
        "ts",
        (_plain("pq_waveform_raw_index", "ts"),),
        # Evidentiary captures (retain_until in the future) are kept (0011's K11-style retention class).
        delete_guard="(t.retain_until IS NULL OR t.retain_until < now())",
        blob_ref_column="blob_ref",
    ),
    "feed_obs": TableSpec("feed_obs", "ts", (_plain("feed_obs", "ts"),)),
    "plan_energy_value": TableSpec(
        "plan_energy_value", "created_at", (_plain("plan_energy_value", "created_at"),)
    ),
    "lifecycle_run": TableSpec("lifecycle_run", "started_at", ()),
}

#: Partitioned by UTC day; must match og.lifecycle_partition_column() in migration 0033.
PARTITIONED_TABLES: dict[str, str] = {"telemetry": "ts", "telemetry_1m": "bucket"}

#: Monthly write-once billing export (06 S4.4 "monthly signed export"); never deleted.
BILLING_ARCHIVES: tuple[ArchiveSpec, ...] = (
    ArchiveSpec(
        name="billing_invoice_line",
        like_table="invoice_line",
        select_sql="SELECT * FROM og.invoice_line WHERE created_at >= %(lo)s AND created_at < %(hi)s",
        ts_sql="SELECT count(*), min(created_at), max(created_at) FROM og.invoice_line "
        "WHERE created_at >= %(lo)s AND created_at < %(hi)s",
    ),
    ArchiveSpec(
        name="billing_pnl",
        like_table="pnl",
        select_sql="SELECT * FROM og.pnl WHERE created_at >= %(lo)s AND created_at < %(hi)s",
        ts_sql="SELECT count(*), min(created_at), max(created_at) FROM og.pnl "
        "WHERE created_at >= %(lo)s AND created_at < %(hi)s",
    ),
)


def archive_spec(name: str) -> ArchiveSpec:
    """Look up any archive (table day export or billing) by name; KeyError if unknown."""
    for spec in DELETABLE_TABLES.values():
        for arch in spec.archives:
            if arch.name == name:
                return arch
    for arch in BILLING_ARCHIVES:
        if arch.name == name:
            return arch
    raise KeyError(name)


def day_start(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=UTC)


def utc_today(now: datetime) -> date:
    return now.astimezone(UTC).date()


def drop_cutoff(now: datetime, keep_days: int) -> datetime:
    """Rows/partitions ending at or before this instant are past retention."""
    return day_start(utc_today(now)) - timedelta(days=keep_days)


def last_exportable_day(now: datetime, hot_days: int | None) -> date | None:
    """The newest closed day the hot->cold pre-export may write, or None when pre-export is off."""
    if hot_days is None:
        return None
    return utc_today(now) - timedelta(days=max(hot_days, 1))


def days_between(first: date, last: date) -> list[date]:
    """Inclusive list of days first..last (empty when first > last)."""
    count = (last - first).days + 1
    return [first + timedelta(days=i) for i in range(max(count, 0))]


def can_delete(row: RetentionRow) -> bool:
    """A table is deleted from only if it is whitelisted, has a finite keep_days, is not protected and
    has no legal hold."""
    return (
        row.table_name in DELETABLE_TABLES
        and row.mode in ("PARTITION", "DELETE")
        and row.keep_days is not None
        and not row.protected
        and not row.legal_hold
    )


def month_bounds(month_start: date) -> tuple[datetime, datetime]:
    nxt = date(month_start.year + (month_start.month == 12), month_start.month % 12 + 1, 1)
    return day_start(month_start), day_start(nxt)


def closed_months(first: date, now: datetime, grace_days: int = 1) -> list[date]:
    """First days of every month from `first`'s month whose end is at least `grace_days` in the past."""
    months: list[date] = []
    cursor = date(first.year, first.month, 1)
    limit = utc_today(now) - timedelta(days=grace_days)
    while True:
        _lo, hi = month_bounds(cursor)
        if utc_today(hi) > limit:
            break
        months.append(cursor)
        cursor = utc_today(hi)
    return months


def disk_level(used_pct: float, cfg: LifecycleConfig) -> str:
    """RB-044 thresholds (ALR-162 levels, tightened for the PostgreSQL volume): OK / WARN / CRIT."""
    if used_pct >= cfg.disk_crit_pct:
        return "CRIT"
    if used_pct >= cfg.disk_warn_pct:
        return "WARN"
    return "OK"
