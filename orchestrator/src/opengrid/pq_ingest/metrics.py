"""Prometheus metrics for `opengrid.pq_ingest`'s in-memory summary flush buffer (07-delivery/06 S6.5,
S9 wave-2 build report: fixing the per-message-insert pattern that stalled the live fleet).

Kept local to this package rather than added to `opengrid.platform.metrics` (an architect-owned file
this package does not edit, BUILD.md S4) -- every other `og-*` process's metrics live in that one
catalogue, so the wave-2 build report flags these names for the architect to fold in there later if a
single central catalogue is wanted; until then this mirrors the catalogue's own naming convention
(`og_<area>_<noun>[_total]`) so a fold-in is a rename, not a redesign.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge

summaries_buffered = Gauge(
    "og_pq_ingest_summaries_buffered",
    "Waveform summaries currently held in opengrid.pq_ingest's in-memory flush buffer.",
)
summaries_dropped_total = Counter(
    "og_pq_ingest_summaries_dropped_total",
    "Waveform summaries dropped oldest-first because the flush buffer was at its capacity.",
)
summaries_flushed_total = Counter(
    "og_pq_ingest_summaries_flushed_total",
    "Waveform summaries persisted via a batched insert_summaries_batch() flush.",
)
summaries_flush_failed_total = Counter(
    "og_pq_ingest_summaries_flush_failed_total",
    "Batched summary flushes that raised (the rows were requeued, subject to the buffer cap).",
)
