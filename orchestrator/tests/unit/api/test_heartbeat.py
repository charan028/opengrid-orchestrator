"""og-api process heartbeat (02b S6.4): regression for the live 2026-09-26 finding that og-api never
wrote one, so health kept `ALR-PROCESS-DOWN` open for `api` permanently."""

from __future__ import annotations

import pytest

from opengrid.api.app import heartbeat_loop


class _StopLoopError(Exception):
    pass


async def test_heartbeat_loop_writes_the_api_heartbeat_each_interval_and_survives_a_failed_write() -> None:
    writes: list[str] = []
    sleeps: list[float] = []

    async def _write(pool, process):
        writes.append(process)
        if len(writes) == 1:
            raise RuntimeError("db hiccup")

    async def _sleep(interval_s):
        sleeps.append(interval_s)
        if len(sleeps) == 3:
            raise _StopLoopError

    with pytest.raises(_StopLoopError):
        await heartbeat_loop(object(), interval_s=5.0, write=_write, sleep=_sleep)  # type: ignore[arg-type]

    assert writes == ["api", "api", "api"]
    assert sleeps == [5.0, 5.0, 5.0]
