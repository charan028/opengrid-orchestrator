"""Regression test for the R2 live bug (2026-09-26): `PgDriftObservationRepo.observation_window` used
to read `rows[-1]` for "the latest measured offset", but `pq_ingest.latest_summaries`/`PgPqIngestBackend.
latest_summaries` return rows NEWEST FIRST (`ORDER BY hub_id, ts DESC`) -- so `rows[-1]` was actually the
OLDEST row in the window, from before any drift. hub-01996 measured 0.017 Hz pre-calibration offset
against a real 0.2 Hz drift; the resulting (wrong) correction then read `WORSE_ROLLED_BACK` on
verification. This test fixes an ordered fixture EXACTLY the way the real backend returns it (newest
first) and asserts the picked offset is the newest row's, not the last list element's -- so a future
re-introduction of `rows[-1]`-without-a-sort would fail this test immediately.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from opengrid.assets import repo as repo_module
from opengrid.core.models.pq import PqWaveformSummaryRow

HUB_ID = "hub-01996"
NOW = datetime(2026, 9, 26, 15, 0, 0, tzinfo=UTC)


def _summary(ts: datetime, freq_hz: float) -> PqWaveformSummaryRow:
    return PqWaveformSummaryRow(hub_id=HUB_ID, ts=ts, freq_hz=Decimal(str(freq_hz)), v_rms_a=Decimal("240.0"))


class _FakePool:
    """A pool stand-in whose `.connection()` context manager's cursor answers the one query
    `observation_window` issues against `og.hub_inverter_pq` (the characterization row) -- no real
    Postgres needed for this regression test; the bug is in Python-level list handling, not SQL."""

    def __init__(self, char_row: tuple[object, ...]) -> None:
        self._char_row = char_row

    def connection(self):
        return _FakeConnCtx(self._char_row)


class _FakeConnCtx:
    def __init__(self, char_row: tuple[object, ...]) -> None:
        self._char_row = char_row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def cursor(self):
        return _FakeCursorCtx(self._char_row)


class _FakeCursorCtx:
    def __init__(self, char_row: tuple[object, ...]) -> None:
        self._char_row = char_row

    async def __aenter__(self):
        return _FakeCursor(self._char_row)

    async def __aexit__(self, *exc):
        return False


class _FakeCursor:
    def __init__(self, char_row: tuple[object, ...]) -> None:
        self._char_row = char_row

    async def execute(self, *_args, **_kwargs) -> None:
        return None

    async def fetchone(self):
        return self._char_row


@pytest.fixture
def characterization_row() -> tuple[object, ...]:
    # freq_offset_hz, freq_offset_std_hz, voltage_offset_pct, voltage_offset_std_pct, thd_current_pct,
    # phase_angle_error_deg -- matches `_CHARACTERIZATION_SQL`'s column order.
    return (0.0, 0.01, 0.0, 0.5, 3.0, 0.0)


async def test_observation_window_selects_offset_from_newest_row_not_last_list_element(
    monkeypatch: pytest.MonkeyPatch, characterization_row: tuple[object, ...]
) -> None:
    """Fixture rows are handed to the code EXACTLY as the real backend returns them: newest first. The
    newest row (60.2 Hz, 12 minutes ago) is a real 0.2 Hz drift; the oldest row (60.017 Hz, 14 minutes
    ago -- the pre-drift baseline) is what the R2 bug picked instead."""
    newest = _summary(NOW - timedelta(minutes=12), freq_hz=60.2)
    oldest = _summary(NOW - timedelta(minutes=14), freq_hz=60.017)
    rows_newest_first = [newest, oldest]

    async def fake_latest_summaries(hub_ids, *, since):
        assert hub_ids == [HUB_ID]
        return rows_newest_first

    monkeypatch.setattr(repo_module.pq_ingest, "latest_summaries", fake_latest_summaries)

    drift_repo = repo_module.PgDriftObservationRepo(_FakePool(characterization_row))
    window = await drift_repo.observation_window(HUB_ID)

    assert window is not None
    # The R2 bug would have produced 0.017 here (60.017 - 60.0) -- the OLDEST row, picked via `rows[-1]`
    # on a newest-first list.
    assert window.latest_measured_offset.freq_hz == pytest.approx(0.2)


async def test_observation_window_correct_even_if_backend_ever_returns_oldest_first(
    monkeypatch: pytest.MonkeyPatch, characterization_row: tuple[object, ...]
) -> None:
    """The fix sorts explicitly by `ts` rather than trusting any particular backend order -- so the same
    assertion holds even if a future backend change (or a differently-behaving fake in another test)
    returns rows oldest-first instead."""
    newest = _summary(NOW - timedelta(minutes=12), freq_hz=60.2)
    oldest = _summary(NOW - timedelta(minutes=14), freq_hz=60.017)
    rows_oldest_first = [oldest, newest]

    async def fake_latest_summaries(hub_ids, *, since):
        return rows_oldest_first

    monkeypatch.setattr(repo_module.pq_ingest, "latest_summaries", fake_latest_summaries)

    drift_repo = repo_module.PgDriftObservationRepo(_FakePool(characterization_row))
    window = await drift_repo.observation_window(HUB_ID)

    assert window is not None
    assert window.latest_measured_offset.freq_hz == pytest.approx(0.2)
