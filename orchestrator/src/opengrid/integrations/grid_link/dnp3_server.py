"""DNP3 outstation listener for the grid link: TCP or mutual TLS, peer allow-list, link layer and transport
(grid-link.md S4.1, S5.1). The application layer is `dnp3_session.OutstationSession`.

Admission of an association, in order (every refusal closes the socket and is traced as AUTHZ_DENY):

1. the peer IP must be inside the utility's `allowed_peers`;
2. with TLS, the handshake itself requires a certificate from the utility's CA, and the certificate CN must
   be in `allowed_peer_cns`;
3. at most `max_associations` concurrent associations (primary + backup control centre).

Link frames must be addressed from the configured master address to the configured outstation address;
anything else is dropped. Multi-fragment responses wait for the master's application CONFIRM between
fragments, as IEEE 1815 requires.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
import time
from collections.abc import Callable
from typing import Any

from opengrid.integrations.grid_link.config import UtilityLinkSettings
from opengrid.integrations.grid_link.dnp3_session import OutstationSession
from opengrid.integrations.grid_link.points import GridLinkPointMap
from opengrid.integrations.grid_link.service import GridLinkService
from opengrid.integrations.scada_dnp3.codec import (
    APP_CON,
    FC_CONFIRM,
    LINK_ACK,
    LINK_CONFIRMED_USER_DATA,
    LINK_REQUEST_LINK_STATUS,
    LINK_RESET_LINK_STATES,
    LINK_TEST_LINK_STATES,
    LINK_UNCONFIRMED_USER_DATA,
    PRM,
    Dnp3ParseError,
    LinkParser,
    Reassembler,
    build_link_frame,
    segment_fragment,
)
from opengrid.integrations.tls import build_server_ssl_context, peer_common_name

logger = logging.getLogger(__name__)

__all__ = ["Dnp3GridLinkServer"]

_READ_CHUNK = 4096
_LINK_STATUS = 0x0B  # secondary function: LINK_STATUS
#: Wait for the master's CONFIRM of a non-final response fragment (IEEE 1815 application timeout).
CONFIRM_TIMEOUT_S = 5.0


class Dnp3GridLinkServer:
    """Listens for one utility's EMS (master) associations and serves `service`."""

    def __init__(
        self,
        settings: UtilityLinkSettings,
        service: GridLinkService,
        *,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._settings = settings
        self._service = service
        self._points = GridLinkPointMap([t.name for t in settings.l2_targets], settings.banks)
        self._monotonic = monotonic or time.monotonic
        self._server: asyncio.Server | None = None
        self._associations: set[asyncio.Task[Any]] = set()

    async def start(self) -> tuple[str, int]:
        """Bind and listen; returns the bound (host, port). Raises on TLS misconfiguration."""
        context = build_server_ssl_context(self._settings.tls)
        self._server = await asyncio.start_server(
            self._accept, self._settings.listen_host, self._settings.listen_port, ssl=context
        )
        host, port = self._server.sockets[0].getsockname()[:2]
        logger.info(
            "grid link listening",
            extra={"utility_id": self._settings.utility_id, "port": port, "tls": context is not None},
        )
        return str(host), int(port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        for task in list(self._associations):
            task.cancel()
        for task in list(self._associations):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None

    @property
    def association_count(self) -> int:
        return len(self._associations)

    # -- admission --------------------------------------------------------------------------------------

    def _admit(self, writer: asyncio.StreamWriter) -> tuple[str, str | None]:
        """(peer, refusal reason or None)."""
        peername = writer.get_extra_info("peername")
        peer = str(peername[0]) if peername else "unknown"
        if not self._settings.peer_allowed(peer):
            return peer, "peer-not-allowed"
        if self._settings.tls.enabled:
            common_name = peer_common_name(writer.get_extra_info("peercert"))
            if common_name is None or common_name not in self._settings.allowed_peer_cns:
                return peer, "peer-cn-not-allowed"
        if len(self._associations) >= self._settings.dnp3.max_associations:
            return peer, "too-many-associations"
        return peer, None

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer, refusal = self._admit(writer)
        if refusal is not None:
            self._service.deny(peer, refusal, "association refused")
            await _close(writer)
            return
        task = asyncio.current_task()
        if task is not None:
            self._associations.add(task)
        logger.info("grid link association", extra={"utility_id": self._settings.utility_id, "peer": peer})
        try:
            await self._serve(reader, writer, peer)
        except (ConnectionError, OSError, ssl.SSLError, TimeoutError) as exc:
            logger.warning(
                "grid link association dropped",
                extra={"utility_id": self._settings.utility_id, "peer": peer, "error": str(exc)},
            )
        finally:
            if task is not None:
                self._associations.discard(task)
            await _close(writer)

    # -- link / transport -------------------------------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, peer: str) -> None:
        dnp3 = self._settings.dnp3
        session = OutstationSession(
            self._points,
            dnp3,
            validate=self._service.validate,
            offer=lambda command: self._service.offer(command, peer),
            status=self._service.status,
            monotonic=self._monotonic,
            max_setpoint_kw=self._settings.max_setpoint_kw,
            max_duration_min=self._settings.max_duration_min,
        )
        link = _Link(writer, outstation=dnp3.outstation_address, master=dnp3.master_address)
        parser, reassembler = LinkParser(), Reassembler(max_fragment=dnp3.max_fragment_size)
        pending: list[bytes] = []  # response fragments waiting for the master's CONFIRM
        while data := await reader.read(_READ_CHUNK):
            for frame in parser.feed(data):
                if frame.destination != dnp3.outstation_address or frame.source != dnp3.master_address:
                    continue
                if not frame.is_primary:
                    continue
                request = await link.on_primary(frame.function, frame.data, reassembler)
                if request is None:
                    continue
                if len(request) >= 2 and request[1] == FC_CONFIRM:
                    if pending and (request[0] & 0x0F) == (pending[0][0] & 0x0F):
                        pending.pop(0)
                        if pending:
                            await link.send(pending[0])
                    continue
                pending = session.process(request)
                if pending:
                    await link.send(pending[0])
                    if not pending[0][0] & APP_CON:
                        pending = []


class _Link:
    """Link-layer replies and outbound transport segmentation for one association."""

    def __init__(self, writer: asyncio.StreamWriter, *, outstation: int, master: int) -> None:
        self._writer = writer
        self._outstation = outstation
        self._master = master
        self._tseq = 0

    async def _write(self, control: int, data: bytes = b"") -> None:
        self._writer.write(build_link_frame(control, self._master, self._outstation, data))
        await asyncio.wait_for(self._writer.drain(), timeout=CONFIRM_TIMEOUT_S)

    async def on_primary(self, function: int, data: bytes, reassembler: Reassembler) -> bytes | None:
        """Answer link services; return a complete application fragment when one is reassembled."""
        if function == LINK_REQUEST_LINK_STATUS:
            await self._write(_LINK_STATUS)
            return None
        if function in (LINK_RESET_LINK_STATES, LINK_TEST_LINK_STATES):
            await self._write(LINK_ACK)
            return None
        if function == LINK_CONFIRMED_USER_DATA:
            await self._write(LINK_ACK)
        elif function != LINK_UNCONFIRMED_USER_DATA:
            return None
        try:
            return reassembler.add(data)
        except Dnp3ParseError:
            logger.warning("grid link dropped an oversized fragment")
            return None

    async def send(self, fragment: bytes) -> None:
        segments, self._tseq = segment_fragment(fragment, self._tseq)
        for segment in segments:
            await self._write(PRM | LINK_UNCONFIRMED_USER_DATA, segment)


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(OSError, ssl.SSLError, ConnectionError):
        await writer.wait_closed()
