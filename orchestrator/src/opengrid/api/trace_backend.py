"""`api`'s trace backend: the canonical `opengrid.trace.pg_backend.PgTraceBackend`, re-exported.

`api` used to carry its own copy of the `TraceBackend` protocol against `og.trace` (written before the
shared backend landed). That copy had no K11 fail-safe journal, so an operator action traced while
Postgres was unreachable failed outright instead of journaling and replaying in order like every other
process's writes. It is now the ONE shared implementation (BUILD.md S1 "no duplicated functions"): the
same hash-chain I/O, the same local journal (`journal_path`, resolved from `[trace].journal_path` by
`journal_path_from_config`), the same checkpoint-guarded pruning.

This module stays as `api`'s import point so `opengrid.api.app` and the API integration fixtures keep
importing `opengrid.api.trace_backend.PgTraceBackend` unchanged. Construct it with the configured
journal: `PgTraceBackend(pool, journal_path=journal_path_from_config(cfg))` -- og-api runs under
`ProtectSystem=strict`, so only the configured `/var/lib/opengrid` path is writable, never the relative
default.
"""

from __future__ import annotations

from opengrid.trace.pg_backend import (
    DEFAULT_JOURNAL_PATH,
    DEFAULT_RETENTION_DAYS,
    PgTraceBackend,
    TraceAppendConflictError,
    TraceJournalUnavailableError,
    journal_path_from_config,
)

__all__ = [
    "DEFAULT_JOURNAL_PATH",
    "DEFAULT_RETENTION_DAYS",
    "PgTraceBackend",
    "TraceAppendConflictError",
    "TraceJournalUnavailableError",
    "journal_path_from_config",
]
