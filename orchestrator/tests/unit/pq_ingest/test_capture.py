"""opengrid.pq_ingest.capture: WaveformCaptureRequest building and pending-request
bookkeeping (S6.4b/S6.6)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from opengrid.pq_ingest.capture import PendingCaptureTracker, build_capture_request


def test_build_capture_request_defaults() -> None:
    now = datetime(2026, 9, 26, tzinfo=UTC)
    request = build_capture_request("hub-00000", "API_REQUEST", now=now)
    assert request.hub_id == "hub-00000"
    assert request.trigger_reason == "API_REQUEST"
    assert request.issued_at == now
    assert request.expires_at == now + timedelta(seconds=5.0)


def test_build_capture_request_custom_ttl() -> None:
    now = datetime(2026, 9, 26, tzinfo=UTC)
    request = build_capture_request("hub-00000", "PQ_DEVIATION", now=now, ttl_s=2.0)
    assert request.expires_at == now + timedelta(seconds=2.0)


def test_tracker_resolve_returns_tracked_request() -> None:
    now = datetime(2026, 9, 26, tzinfo=UTC)
    request = build_capture_request("hub-00000", "API_REQUEST", now=now)
    tracker = PendingCaptureTracker()
    tracker.track(request)
    assert tracker.resolve("hub-00000") is request
    assert tracker.resolve("hub-00000") is None  # popped, resolved only once


def test_tracker_resolve_unknown_hub_returns_none() -> None:
    tracker = PendingCaptureTracker()
    assert tracker.resolve("hub-99999") is None


def test_tracker_expire_stale_drops_only_expired() -> None:
    now = datetime(2026, 9, 26, tzinfo=UTC)
    fresh = build_capture_request("hub-00000", "API_REQUEST", now=now, ttl_s=100.0)
    stale = build_capture_request("hub-00001", "API_REQUEST", now=now, ttl_s=1.0)
    tracker = PendingCaptureTracker()
    tracker.track(fresh)
    tracker.track(stale)

    expired = tracker.expire_stale(now + timedelta(seconds=5.0))

    assert expired == [stale]
    assert tracker.resolve("hub-00001") is None
    assert tracker.resolve("hub-00000") is fresh
