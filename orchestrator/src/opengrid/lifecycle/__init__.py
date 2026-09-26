"""Data lifecycle (docs/orchestrator/07-delivery/15-data-lifecycle.md; migration 0033).

Keeps the node's PostgreSQL bounded: daily partitions for og.telemetry / og.telemetry_1m, retention per
`og.data_retention` (partition drops or batched deletes), hot->cold export with verified checksums before
any drop, 1-min/15-min rollups as the long-lived M&V source, the monthly write-once billing export, an audit
restore helper and a status report. Trace pruning is not here (opengrid.trace, K11).

Modules: `policy` (pure rules and the table whitelist), `partitions`, `archive`, `retention`, `rollup`,
`indexes`, `status`, `runner` (`run_lifecycle`, the single entry point) and `__main__` (CLI).
"""

from opengrid.lifecycle.runner import run_lifecycle

__all__ = ["run_lifecycle"]
