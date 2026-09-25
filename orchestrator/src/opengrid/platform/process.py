"""Common async main-loop helper with graceful shutdown, used by every process entry point.

02b S1.1-S1.3. Handles SIGTERM/SIGINT by cancelling the loop and letting the process exit 0 quickly
(systemd `Restart=always` brings it back). Each process passes a single async `tick()` callable that
does one cycle of its own work; `run_forever` owns the sleep/interval/shutdown plumbing so no process
hand-rolls its own signal handling (K7: degrade, don't trip -- a process that traps its own SIGTERM
badly risks a stuck process instead of a clean, fast restart).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

TickFn = Callable[[], Awaitable[None]]


async def run_forever(tick: TickFn, *, interval_s: float, process_name: str) -> None:
    """Call `tick()` every `interval_s` seconds until SIGTERM/SIGINT, then return.

    A tick that raises is logged and swallowed (the loop keeps running) except for
    `asyncio.CancelledError`, which propagates to stop the loop -- one bad cycle must not crash the
    whole process (K7), but a deliberate shutdown must still be prompt.
    """
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop(sig_name: str) -> None:
        # PLAT-006: "process" collides with the LogRecord's OWN reserved `process` attribute (the OS
        # PID) -- passing it via `extra` makes `makeRecord` raise KeyError instead of logging, which
        # would have silently defeated run_forever's whole "log and keep going" contract below.
        logger.info("shutdown requested", extra={"signal": sig_name, "proc_name": process_name})
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # Windows dev boxes lack some signal handlers
            loop.add_signal_handler(sig, _request_stop, sig.name)

    while not stop_event.is_set():
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("tick failed", extra={"proc_name": process_name})

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)

    logger.info("stopped", extra={"proc_name": process_name})
