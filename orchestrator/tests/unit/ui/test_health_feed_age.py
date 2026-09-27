"""System Health feed ages: a forecast product's latest value is timestamped in the future, and must read
"ahead: ..." (the live badges' wording), never a negative age like "-591633s" (r3.4.3, seen on the demo)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from opengrid.ui.routes.health import _feed_row, format_age


def test_future_ages_read_ahead() -> None:
    assert format_age(-30) == "ahead: 30s"
    assert format_age(-2075) == "ahead: 34m"
    assert format_age(-591633) == "ahead: 164h 20m"


def test_past_ages_are_unchanged() -> None:
    assert format_age(0) == "0s"
    assert format_age(5675) == "1h 34m"


def test_a_future_stamped_feed_row_reads_ahead() -> None:
    future = (datetime.now(UTC) + timedelta(days=6)).isoformat()
    row = _feed_row({"source": "ERCOT", "product": "np4-745-cd", "quality": "GOOD", "last_value_at": future})
    assert row["age_display"].startswith("ahead: ")
