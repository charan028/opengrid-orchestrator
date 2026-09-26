"""K11 external anchoring: publish failures raise a health alert, not just a log line (R2 review).

`opengrid.trace.anchoring.publish_anchor` raises when the PRIMARY anchor copy cannot be written (e.g. a
relative or read-only `[trace].anchor_dir` under `ProtectSystem=strict`, the live R2 defect) and returns
`secondary_path=None` when only the secondary copy failed. Both now surface through the same `og.alert`
mechanism `opengrid.health` and `invariants.trace_verify` use -- raised once per condition, cleared when a
publish succeeds with both copies.
"""

from __future__ import annotations

from datetime import UTC, datetime

from psycopg_pool import AsyncConnectionPool

from opengrid.health.model import AlertFinding, AlertSeverity
from opengrid.health.queries import clear_alert, fetch_open_alerts, raise_alert

ALR_ANCHOR_PUBLISH_FAILED = "ALR-ANCHOR-PUBLISH-FAILED"
ALR_ANCHOR_SECONDARY_FAILED = "ALR-ANCHOR-SECONDARY-FAILED"


async def record_publish_outcome(
    pool: AsyncConnectionPool,
    *,
    primary_error: str | None,
    secondary_written: bool,
    now: datetime | None = None,
) -> None:
    """Raise/clear the two anchor alerts for one publish attempt: `primary_error` set -> critical
    ALR-ANCHOR-PUBLISH-FAILED (nothing was anchored); primary ok but no secondary copy -> warning
    ALR-ANCHOR-SECONDARY-FAILED; each clears once its condition is gone."""
    now = now or datetime.now(UTC)
    open_alerts = await fetch_open_alerts(pool)
    checks: tuple[tuple[str, AlertSeverity, bool, str], ...] = (
        (
            ALR_ANCHOR_PUBLISH_FAILED,
            "critical",
            primary_error is not None,
            f"K11 trace anchor could not be published: {primary_error}",
        ),
        (
            ALR_ANCHOR_SECONDARY_FAILED,
            "warning",
            primary_error is None and not secondary_written,
            "K11 trace anchor published without its secondary copy",
        ),
    )
    for rule, severity, failing, summary in checks:
        open_for_rule = [a for a in open_alerts if a.rule == rule]
        if failing:
            if not open_for_rule:
                await raise_alert(
                    pool,
                    AlertFinding(
                        rule=rule,
                        severity=severity,
                        summary=summary,
                        condition_key=rule,
                        detail={"scope_kind": "PROCESS", "scope_ref": "settle", "error": primary_error},
                    ),
                    opened_at=now,
                )
        else:
            for alert in open_for_rule:
                if alert.id is not None:
                    await clear_alert(pool, alert.id, cleared_at=now)
