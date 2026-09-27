"""R3.4.3 workstation-review LOWs on the R3.4.1 PROD-IO fix (f7510ab):

- L-5: a cancelled journal write released the journal lock while its thread was still writing, letting a
  concurrent reader/rewriter interleave with an unfinished write. `_run_in_thread_uncancellable` must run
  the thread to completion regardless of the awaiting coroutine's cancellation.
- L-6: `pending_count()`'s lock acquisition was a plain blocking `flock()` -- a latent deadlock if held by
  an async holder itself waiting on the event loop to resume it. It must give up loudly instead.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time

import pytest

from opengrid.trace.pg_backend import (
    JournalLockTimeoutError,
    _journal_lock_sync,
    _run_in_thread_uncancellable,
)

pytestmark = pytest.mark.asyncio


async def test_run_in_thread_uncancellable_completes_even_if_the_caller_is_cancelled() -> None:
    finished = threading.Event()

    def _slow_write() -> None:
        time.sleep(0.1)
        finished.set()

    task = asyncio.ensure_future(_run_in_thread_uncancellable(_slow_write))
    await asyncio.sleep(0.02)  # let the thread actually start before cancelling
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # The whole point of the fix: by the time the CancelledError has propagated out, the thread must
    # already have finished -- never abandoned mid-write.
    assert finished.is_set()


async def test_run_in_thread_uncancellable_runs_normally_when_not_cancelled() -> None:
    calls: list[int] = []
    await _run_in_thread_uncancellable(lambda: calls.append(1))
    assert calls == [1]


@pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX-only")
def test_pending_count_lock_gives_up_loudly_instead_of_deadlocking(tmp_path, monkeypatch) -> None:
    """A blocking flock() here could never return control to the event loop if the current holder was an
    async coroutine itself waiting on the loop to resume it (e.g. a shielded to_thread write) -- a real
    deadlock. Bounded and non-blocking instead: raise within a short, patched timeout."""
    journal_path = tmp_path / "journal.jsonl"
    monkeypatch.setattr("opengrid.trace.pg_backend._LOCK_SYNC_TIMEOUT_S", 0.05)

    with (
        _journal_lock_sync(journal_path, exclusive=True),
        pytest.raises(JournalLockTimeoutError),
        _journal_lock_sync(journal_path, exclusive=True),
    ):
        pass  # pragma: no cover -- must never be entered while the outer lock is held
