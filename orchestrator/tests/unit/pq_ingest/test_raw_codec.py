"""opengrid.pq_ingest.raw_codec: int16 sample block encode/decode round trip."""

from __future__ import annotations

from opengrid.pq_ingest.raw_codec import decode_raw_samples, encode_raw_samples


def test_round_trip_two_channels() -> None:
    channels = ["V", "I"]
    samples = {"V": [0, 100, -100, 32767, -32768], "I": [1, 2, 3, 4, 5]}
    blob = encode_raw_samples(channels, samples)
    assert decode_raw_samples(channels, blob) == samples


def test_round_trip_six_channels() -> None:
    channels = ["V_A", "V_B", "V_C", "I_A", "I_B", "I_C"]
    samples = {c: [i, -i, i * 2] for i, c in enumerate(channels, start=1)}
    blob = encode_raw_samples(channels, samples)
    assert decode_raw_samples(channels, blob) == samples


def test_encode_length_matches_two_bytes_per_sample() -> None:
    channels = ["V", "I"]
    samples = {"V": [1, 2, 3], "I": [4, 5, 6]}
    blob = encode_raw_samples(channels, samples)
    assert len(blob) == 2 * 3 * 2  # 2 channels * 3 samples * 2 bytes/int16


def test_decode_empty_channels_returns_empty_dict() -> None:
    assert decode_raw_samples([], b"") == {}


def test_decode_rejects_length_not_divisible_by_channel_count() -> None:
    import pytest

    with pytest.raises(ValueError, match="does not divide evenly"):
        decode_raw_samples(["V", "I"], b"\x00\x00\x00")
