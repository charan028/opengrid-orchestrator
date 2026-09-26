"""`python -m opengrid.lifecycle status`: per-table size, rows, partitions, oldest row, next drop, disk
headroom and the last runs. Read-only; every potentially slow probe runs under a short statement_timeout.

Alert thresholds (RB-044; ALR-162 levels, tightened for the PostgreSQL volume): disk used >= 80 % WARN,
>= 90 % CRIT (`[lifecycle].disk_warn_pct` / `disk_crit_pct`); cold tier free < `cold_min_free_gb` blocks
exports (and therefore drops) and is CRIT; the last hourly run not ok, or older than 2 h, is WARN.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from opengrid.lifecycle.policy import DELETABLE_TABLES, PARTITIONED_TABLES, LifecycleConfig, disk_level

_PROBE_TIMEOUT = "3s"

_TABLE_SIZE_SQL = """
SELECT coalesce(sum(pg_total_relation_size(t.relid)), 0)::bigint,
       coalesce(sum(greatest(c.reltuples, 0)), 0)::bigint,
       count(*) FILTER (WHERE t.isleaf AND t.level > 0)
FROM pg_partition_tree(%(rel)s::regclass) t JOIN pg_class c ON c.oid = t.relid
"""


def _disk(path: Path, cfg: LifecycleConfig) -> dict[str, Any]:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    used_pct = 100.0 * usage.used / usage.total if usage.total else 0.0
    return {
        "path": str(path),
        "total_gb": round(usage.total / 1e9, 1),
        "free_gb": round(usage.free / 1e9, 1),
        "used_pct": round(used_pct, 1),
        "level": disk_level(used_pct, cfg),
    }


async def _probe(
    conn: psycopg.AsyncConnection[Any], stmt: sql.Composable | str, params: dict[str, Any]
) -> Any:
    try:
        async with conn.transaction():
            await conn.execute(f"SET LOCAL statement_timeout = '{_PROBE_TIMEOUT}'")
            cur = await conn.execute(stmt, params)  # type: ignore[arg-type]
            row = await cur.fetchone()
            return row[0] if row else None
    except psycopg.errors.QueryCanceled:
        return "timeout"


async def collect_status(
    pool: AsyncConnectionPool, cfg: LifecycleConfig, now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    tables: list[dict[str, Any]] = []
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT table_name, ts_column, mode, hot_days, keep_days, archive, legal_hold, protected "
            "FROM og.data_retention ORDER BY table_name"
        )
        policies = await cur.fetchall()
        for name, ts_col, mode, hot, keep, archive, hold, protected in policies:
            entry: dict[str, Any] = {
                "table": name,
                "mode": mode,
                "hot_days": hot,
                "keep_days": keep,
                "archive": archive,
                "legal_hold": hold,
                "protected": protected,
            }
            reg = await (await conn.execute("SELECT to_regclass(%s)", (f'og."{name}"',))).fetchone()
            if reg is None or reg[0] is None:
                entry["missing"] = True
                tables.append(entry)
                continue
            size = await (await conn.execute(_TABLE_SIZE_SQL, {"rel": f'og."{name}"'})).fetchone()
            if size is None:
                continue
            entry.update({"bytes": int(size[0]), "est_rows": int(size[1]), "partitions": int(size[2])})
            oldest: Any
            if name in PARTITIONED_TABLES:
                part_cur = await conn.execute(
                    "SELECT min(upper_ts) FILTER (WHERE NOT is_default), min(lower_ts) FILTER (WHERE NOT is_default), "
                    "bool_or(lower_ts IS NULL AND NOT is_default) FROM og.lifecycle_partitions WHERE parent = %s",
                    (name,),
                )
                prow = await part_cur.fetchone()
                if prow is None:
                    continue
                oldest_upper, oldest_lower, has_legacy = prow
                oldest = "legacy range (MINVALUE)" if has_legacy else oldest_lower
                if keep is not None and oldest_upper is not None:
                    entry["next_drop_at"] = (oldest_upper + timedelta(days=keep)).isoformat()
            else:
                oldest = await _probe(
                    conn,
                    sql.SQL("SELECT min({}) FROM og.{}").format(sql.Identifier(ts_col), sql.Identifier(name)),
                    {},
                )
                if keep is not None and isinstance(oldest, datetime):
                    day_end = datetime.combine(oldest.astimezone(UTC).date(), datetime.min.time(), tzinfo=UTC)
                    entry["next_drop_at"] = (day_end + timedelta(days=keep + 1)).isoformat()
            entry["oldest"] = oldest.isoformat() if isinstance(oldest, datetime) else oldest
            entry["deletable"] = name in DELETABLE_TABLES and keep is not None and not hold and not protected
            tables.append(entry)

        arch = await (
            await conn.execute("SELECT count(*), coalesce(sum(bytes), 0) FROM og.lifecycle_archive")
        ).fetchone()
        runs = await (
            await conn.execute(
                "SELECT cycle, started_at, finished_at, ok FROM og.lifecycle_run ORDER BY started_at DESC LIMIT 5"
            )
        ).fetchall()
        db = await (await conn.execute("SELECT pg_database_size(current_database())")).fetchone()

    disks = [_disk(cfg.pgdata_path, cfg), _disk(cfg.cold_root, cfg)]
    cold_free = disks[1]["free_gb"]
    if cold_free < cfg.cold_min_free_gb:
        disks[1]["level"] = "CRIT"
    last_hourly = next((r for r in runs if r[0] == "hourly"), None)
    run_level = "OK"
    if last_hourly is None or last_hourly[3] is not True or now - last_hourly[1] > timedelta(hours=2):
        run_level = "WARN"
    levels = [d["level"] for d in disks] + [run_level]
    overall = "CRIT" if "CRIT" in levels else "WARN" if "WARN" in levels else "OK"
    return {
        "at": now.isoformat(),
        "level": overall,
        "database_bytes": int(db[0]) if db else None,
        "tables": tables,
        "disks": disks,
        "archives": {"files": int(arch[0]) if arch else 0, "bytes": int(arch[1]) if arch else 0},
        "runs": [
            {
                "cycle": r[0],
                "started_at": r[1].isoformat(),
                "finished_at": r[2].isoformat() if r[2] else None,
                "ok": r[3],
            }
            for r in runs
        ],
        "run_level": run_level,
    }


def _gb(n: int | None) -> str:
    return "-" if n is None else f"{n / 1e9:8.2f}"


def render_status(status: dict[str, Any]) -> str:
    lines = [f"lifecycle status at {status['at']}: {status['level']}", ""]
    lines.append(
        f"{'table':24} {'mode':9} {'keep':>5} {'GB':>8} {'rows':>13} {'parts':>5}  oldest / next drop"
    )
    for t in status["tables"]:
        if t.get("missing"):
            lines.append(f"{t['table']:24} (missing)")
            continue
        keep = "-" if t["keep_days"] is None else str(t["keep_days"])
        flag = " HOLD" if t["legal_hold"] else (" PROTECTED" if t["protected"] else "")
        lines.append(
            f"{t['table']:24} {t['mode']:9} {keep:>5} {_gb(t['bytes'])} {t['est_rows']:>13,} {t['partitions']:>5}  "
            f"{t.get('oldest')} / {t.get('next_drop_at', '-')}{flag}"
        )
    lines.append("")
    for d in status["disks"]:
        lines.append(
            f"disk {d['path']:22} {d['used_pct']:5.1f}% used, {d['free_gb']:.1f} GB free  [{d['level']}]"
        )
    lines.append(
        f"database size {_gb(status['database_bytes']).strip()} GB; cold archives "
        f"{status['archives']['files']} files, {status['archives']['bytes'] / 1e9:.2f} GB"
    )
    lines.append(f"last runs [{status['run_level']}]:")
    for r in status["runs"]:
        lines.append(f"  {r['cycle']:6} {r['started_at']}  ok={r['ok']}")
    return "\n".join(lines)
