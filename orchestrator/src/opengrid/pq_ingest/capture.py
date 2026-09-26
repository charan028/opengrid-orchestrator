"""On-demand waveform capture requests (07-delivery/06 S6.4b, S6.6): building a
validated `WaveformCaptureRequest` message to publish on
`<root>/scada/wave/<zone>/<bank_id>/<hub_id>/request`, and tracking which requests are
still outstanding so an inbound raw capture can be correlated back to the request that
triggered it (S8.4 TS-15a's round trip).

Pure bookkeeping -- no MQTT, no DB. The caller publishes the built message and calls
`opengrid.pq_ingest.track_capture_request`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from opengrid.core.models.pq import WaveformCaptureRequest, WaveformTriggerReason

# S6.4b gives no fixed request TTL; 5 s comfortably covers a hub's capture (166.7 ms
# window, S6.4a) plus queueing/publish latency without holding a slot open needlessly.
DEFAULT_CAPTURE_TTL_S = 5.0


def build_capture_request(
    hub_id: str,
    trigger_reason: WaveformTriggerReason,
    *,
    now: datetime,
    ttl_s: float = DEFAULT_CAPTURE_TTL_S,
) -> WaveformCaptureRequest:
    """S6.4b: the orchestrator -> hub on-demand capture trigger. `ttl_s` bounds how long
    a hub may take to respond (the schema's `expires_at`) -- the hub itself drops a
    request it cannot answer before that deadline (S6.4b, `pq_waveform_raw.schema.json`'s
    hub-side note)."""
    return WaveformCaptureRequest(
        request_id=uuid.uuid4(),
        hub_id=hub_id,
        trigger_reason=trigger_reason,
        issued_at=now,
        expires_at=now + timedelta(seconds=ttl_s),
    )


@dataclass
class PendingCaptureTracker:
    """Bookkeeping only. Tracks outstanding `WaveformCaptureRequest`s by `hub_id` so a
    caller (WP-J's API layer, or this package's own housekeeping) can correlate an
    inbound raw capture with the request that triggered it, and expire requests the hub
    never answered. Not persisted -- a process restart loses in-flight requests, which is
    acceptable since a lost request only delays a capture the caller can re-issue."""

    _pending: dict[str, WaveformCaptureRequest] = field(default_factory=dict)

    def track(self, request: WaveformCaptureRequest) -> None:
        self._pending[request.hub_id] = request

    def resolve(self, hub_id: str) -> WaveformCaptureRequest | None:
        """Pops and returns the outstanding request for `hub_id`, or `None` if this raw
        capture was not associated with a tracked on-demand request (e.g. a rotating
        audit sample the hub self-triggered)."""
        return self._pending.pop(hub_id, None)

    def expire_stale(self, now: datetime) -> list[WaveformCaptureRequest]:
        expired = [request for request in self._pending.values() if request.expires_at <= now]
        for request in expired:
            self._pending.pop(request.hub_id, None)
        return expired
