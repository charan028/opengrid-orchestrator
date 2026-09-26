"""Binary encoding for a raw waveform capture's sample block (07-delivery/06 S6.4a:
"binary (not base64-in-JSON, which would add ~33% overhead)").

The inbound MQTT payload this package receives is already JSON-decoded by the caller's
ingest loop (matching every other `interfaces/mqtt/*.schema.json` message this codebase
ingests) and carries the sample arrays in `pq_waveform_raw.schema.json`'s `samples`
field -- explicitly documented there as "the test/fixture-only JSON representation ...
never sent as JSON on the wire" for a real device. This module re-encodes that already-
decoded payload into the compact binary form S6.4a specifies for OBJECT STORAGE (distinct
from the DB row, `og.pq_waveform_raw_index`), so what actually sits in the blob store
matches the spec's bandwidth intent even though this sim/test environment's wire
transport is JSON end-to-end.

Format: each channel's `int16` samples, little-endian, concatenated in `channels` order
-- no per-channel length prefix needed since every channel in one capture has the same
sample count (`cycles * sample_rate_hz / 60`, fixed for a given capture).
"""

from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence


def encode_raw_samples(channels: Sequence[str], samples: Mapping[str, Sequence[int]]) -> bytes:
    """Packs `samples[channel]` for every `channel` in `channels`, in order, as
    little-endian int16. A channel absent from `samples` (should not happen for a valid
    capture, but never silently produces a shorter blob) raises `KeyError`."""
    buffer = bytearray()
    for channel in channels:
        values = samples[channel]
        buffer.extend(struct.pack(f"<{len(values)}h", *values))
    return bytes(buffer)


def decode_raw_samples(channels: Sequence[str], blob: bytes) -> dict[str, list[int]]:
    """Inverse of `encode_raw_samples`: splits `blob` back into one int16 list per
    channel. Requires `len(blob)` to divide evenly across `len(channels)` (every channel
    in one capture has the same sample count)."""
    if not channels:
        return {}
    total_int16 = len(blob) // 2
    if total_int16 % len(channels) != 0:
        raise ValueError("raw capture blob length does not divide evenly across its channels")
    per_channel = total_int16 // len(channels)
    values = struct.unpack(f"<{total_int16}h", blob)
    return {
        channel: list(values[i * per_channel : (i + 1) * per_channel]) for i, channel in enumerate(channels)
    }
