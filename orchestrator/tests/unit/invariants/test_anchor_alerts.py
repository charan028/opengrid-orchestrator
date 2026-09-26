"""K11 anchor publish failures surface as health alerts (raise once, clear on a clean publish)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from opengrid.core.models.platform import Alert
from opengrid.invariants import anchor_alerts

NOW = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)


class _AlertDb:
    def __init__(self) -> None:
        self.open: list[Alert] = []
        self.raised: list[Any] = []
        self.cleared: list[int] = []

    async def fetch_open_alerts(self, pool: object) -> list[Alert]:
        return list(self.open)

    async def raise_alert(self, pool: object, finding: Any, *, opened_at: datetime) -> None:
        self.raised.append(finding)
        self.open.append(
            Alert(
                id=len(self.raised),
                rule=finding.rule,
                severity=finding.severity,
                summary="s",
                opened_at=opened_at,
            )
        )

    async def clear_alert(self, pool: object, alert_id: int, *, cleared_at: datetime) -> None:
        self.cleared.append(alert_id)
        self.open = [a for a in self.open if a.id != alert_id]


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> _AlertDb:
    fake = _AlertDb()
    monkeypatch.setattr(anchor_alerts, "fetch_open_alerts", fake.fetch_open_alerts)
    monkeypatch.setattr(anchor_alerts, "raise_alert", fake.raise_alert)
    monkeypatch.setattr(anchor_alerts, "clear_alert", fake.clear_alert)
    return fake


async def test_primary_failure_raises_one_critical_alert_and_a_clean_publish_clears_it(db: _AlertDb) -> None:
    for _ in range(3):  # the same failing condition across runs: raised once, not a storm
        await anchor_alerts.record_publish_outcome(
            object(), primary_error="read-only file system", secondary_written=False, now=NOW
        )
    assert [(f.rule, f.severity) for f in db.raised] == [
        (anchor_alerts.ALR_ANCHOR_PUBLISH_FAILED, "critical")
    ]

    await anchor_alerts.record_publish_outcome(object(), primary_error=None, secondary_written=True, now=NOW)
    assert db.open == []


async def test_missing_secondary_copy_raises_a_warning(db: _AlertDb) -> None:
    await anchor_alerts.record_publish_outcome(object(), primary_error=None, secondary_written=False, now=NOW)
    assert [(f.rule, f.severity) for f in db.raised] == [
        (anchor_alerts.ALR_ANCHOR_SECONDARY_FAILED, "warning")
    ]
