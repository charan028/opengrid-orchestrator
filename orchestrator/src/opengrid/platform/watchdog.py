"""systemd watchdog integration (single-host HA, owner-approved 2026-09-26: `WatchdogSec=` + `sd_notify`
instead of Kubernetes liveness probes). No `python-systemd` dependency -- `sd_notify` is just a datagram
written to the `NOTIFY_SOCKET` the unit manager hands the process in its environment, so a stdlib
`socket` does the whole job (systemd.exec(5) "Notifying Supervision Status").

Every og-* process's main loop calls `notify_watchdog()` once per cycle, well inside the unit's own
`WatchdogSec=` (systemd kills and restarts the unit if it stops calling in time -- that is the point: a
wedged process that still holds its MQTT/DB connections open gets replaced without a human noticing).
None of this does anything when the process was not started under systemd (`NOTIFY_SOCKET` unset) or on
a platform without `AF_UNIX` (Windows dev boxes) -- every function is then a no-op, never an error, so
the same code runs unchanged in a local `.venv` test run and under `deploy/ha`'s unit files.
"""

from __future__ import annotations

import logging
import os
import socket

logger = logging.getLogger(__name__)

_NOTIFY_SOCKET_ENV = "NOTIFY_SOCKET"
_WATCHDOG_USEC_ENV = "WATCHDOG_USEC"
_DEFAULT_MARGIN = 2  # send at 1/2 of WatchdogSec by default (systemd.service(5) recommendation)


def _send(message: str) -> None:
    """Best-effort `sd_notify`: never raises. A missing `NOTIFY_SOCKET` (not run under systemd, e.g.
    local dev or a workspace test run) is the expected, silent common case, not an error."""
    address = os.environ.get(_NOTIFY_SOCKET_ENV)
    if not address:
        return
    if not hasattr(socket, "AF_UNIX"):  # Windows dev boxes (BUILD.md: local Python runs there too)
        return
    # systemd.exec(5): an address starting with "@" is Linux's abstract-namespace socket, encoded as a
    # NUL byte over the wire (never a literal "@" in the address the kernel receives).
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(message.encode(), address)
    except OSError:
        logger.warning("sd_notify send failed", extra={"message": message}, exc_info=True)


def notify_ready() -> None:
    """`READY=1`: tell systemd this process finished startup (pairs with `Type=notify` in the unit)."""
    _send("READY=1")


def notify_watchdog() -> None:
    """`WATCHDOG=1`: the keepalive ping. Call this once per iteration of the process's main loop (engine
    cycle, guardian poll, safestop confirmation-broker tick, settle/health/feeds scheduler tick) -- never
    from inside a rarely-hit code path, or the watchdog stops meaning "the loop is alive"."""
    _send("WATCHDOG=1")


def notify_stopping() -> None:
    """`STOPPING=1`: tell systemd a graceful shutdown is under way, so it doesn't race a restart against
    the process's own cleanup."""
    _send("STOPPING=1")


def watchdog_interval_s(*, margin: int = _DEFAULT_MARGIN) -> float | None:
    """How often to call `notify_watchdog()`, derived from the unit's own `WatchdogSec=` (systemd sets
    `WATCHDOG_USEC` to that value when `WatchdogSec=` is configured and `Type=notify`). `None` when not
    running under a watchdog-enabled unit -- callers should then simply not schedule the ping at all.
    `margin` (default 2) pings at half the deadline, so one missed tick doesn't trip a restart."""
    raw = os.environ.get(_WATCHDOG_USEC_ENV)
    if not raw:
        return None
    try:
        usec = int(raw)
    except ValueError:
        logger.warning("malformed WATCHDOG_USEC", extra={"value": raw})
        return None
    if usec <= 0 or margin <= 0:
        return None
    return (usec / 1_000_000) / margin
