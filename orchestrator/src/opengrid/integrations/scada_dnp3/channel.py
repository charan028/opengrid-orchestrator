"""DNP3 master channel over TCP (optionally TLS, IEC 62351-3 style): the async I/O around `codec`.

It frames a request (application fragment -> transport segments -> link frames with CRC), writes it,
reads link frames until the final response fragment arrives, and sends the application CONFIRM that a
multi-fragment (CON=1) response requires. Unsolicited responses (UNS=1) arriving in between are confirmed
and handed to `on_unsolicited`. Confirmed user data is link-ACKed. One request at a time per
association (DNP3 is strictly request/response on the master side).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
from collections.abc import Callable

from opengrid.integrations.scada_dnp3.codec import (
    APP_CON,
    APP_FIN,
    APP_UNS,
    DIR,
    FC_RESPONSE,
    FC_UNSOLICITED_RESPONSE,
    LINK_ACK,
    LINK_CONFIRMED_USER_DATA,
    LINK_RESET_LINK_STATES,
    LINK_UNCONFIRMED_USER_DATA,
    PRM,
    Dnp3ParseError,
    LinkFrame,
    LinkParser,
    Reassembler,
    build_confirm,
    build_link_frame,
    segment_fragment,
)

logger = logging.getLogger(__name__)

__all__ = ["Dnp3ChannelError", "Dnp3MasterChannel"]

_READ_CHUNK = 4096


class Dnp3ChannelError(Exception):
    """Connection, timeout or framing failure on the DNP3 association."""


class Dnp3MasterChannel:
    """One TCP association from this master (`master_address`) to one outstation."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        master_address: int,
        outstation_address: int,
        response_timeout_s: float = 5.0,
        connect_timeout_s: float = 5.0,
        ssl_context: ssl.SSLContext | None = None,
        server_hostname: str | None = None,
        reset_link_on_connect: bool = False,
        max_fragment_size: int = 2048,
    ) -> None:
        self._host = host
        self._port = port
        self._master = master_address
        self._outstation = outstation_address
        self._response_timeout_s = response_timeout_s
        self._connect_timeout_s = connect_timeout_s
        self._ssl = ssl_context
        self._server_hostname = server_hostname if ssl_context is not None else None
        self._reset_link = reset_link_on_connect
        self._max_fragment_size = max_fragment_size
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._parser = LinkParser()
        self._transport_seq = 0
        self._lock = asyncio.Lock()
        self.on_unsolicited: Callable[[bytes], None] | None = None

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self._host, self._port, ssl=self._ssl, server_hostname=self._server_hostname
                ),
                timeout=self._connect_timeout_s,
            )
        except (OSError, TimeoutError) as exc:
            raise Dnp3ChannelError(f"connect {self._host}:{self._port} failed: {exc}") from exc
        self._parser = LinkParser()
        self._transport_seq = 0
        if self._reset_link:
            await self._write(
                build_link_frame(DIR | PRM | LINK_RESET_LINK_STATES, self._outstation, self._master)
            )
            await self._await_link_ack()

    async def close(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError, ssl.SSLError):
                await writer.wait_closed()

    async def request(self, fragment: bytes) -> list[bytes]:
        """Send one application request fragment; return every solicited response fragment (in order)
        up to and including the one with FIN set."""
        async with self._lock:
            if not self.connected:
                raise Dnp3ChannelError("not connected")
            await self._send_fragment(fragment)
            return await self._collect_response(fragment[0] & 0x0F)

    # -- internals -----------------------------------------------------------------------------------

    async def _write(self, data: bytes) -> None:
        if self._writer is None:
            raise Dnp3ChannelError("not connected")
        try:
            self._writer.write(data)
            await self._writer.drain()
        except (OSError, ssl.SSLError) as exc:
            await self.close()
            raise Dnp3ChannelError(f"write failed: {exc}") from exc

    async def _send_fragment(self, fragment: bytes) -> None:
        segments, self._transport_seq = segment_fragment(fragment, self._transport_seq)
        for segment in segments:
            frame = build_link_frame(
                DIR | PRM | LINK_UNCONFIRMED_USER_DATA, self._outstation, self._master, segment
            )
            await self._write(frame)

    async def _read_frames(self, deadline: float) -> list[LinkFrame]:
        if self._reader is None:
            raise Dnp3ChannelError("not connected")
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise Dnp3ChannelError("response timeout")
        try:
            data = await asyncio.wait_for(self._reader.read(_READ_CHUNK), timeout=remaining)
        except TimeoutError as exc:
            raise Dnp3ChannelError("response timeout") from exc
        except (OSError, ssl.SSLError) as exc:
            await self.close()
            raise Dnp3ChannelError(f"read failed: {exc}") from exc
        if not data:
            await self.close()
            raise Dnp3ChannelError("outstation closed the connection")
        return self._parser.feed(data)

    async def _await_link_ack(self) -> None:
        deadline = asyncio.get_running_loop().time() + self._response_timeout_s
        while True:
            for frame in await self._read_frames(deadline):
                if frame.destination == self._master and not frame.is_primary:
                    return

    async def _collect_response(self, request_seq: int) -> list[bytes]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._response_timeout_s
        reassembler = Reassembler(max_fragment=self._max_fragment_size)
        fragments: list[bytes] = []
        while True:
            for frame in await self._read_frames(deadline):
                if (
                    frame.destination != self._master
                    or frame.source != self._outstation
                    or not frame.is_primary
                ):
                    continue
                if frame.function == LINK_CONFIRMED_USER_DATA:
                    await self._write(build_link_frame(LINK_ACK, self._outstation, self._master))
                elif frame.function != LINK_UNCONFIRMED_USER_DATA:
                    continue
                try:
                    data = reassembler.add(frame.data)
                except Dnp3ParseError as exc:
                    raise Dnp3ChannelError(str(exc)) from exc
                if data is None or len(data) < 2 or data[1] not in (FC_RESPONSE, FC_UNSOLICITED_RESPONSE):
                    continue
                control = data[0]
                unsolicited = bool(control & APP_UNS)
                if control & APP_CON:
                    await self._send_fragment(build_confirm(control & 0x0F, unsolicited=unsolicited))
                if unsolicited:
                    if self.on_unsolicited is not None:
                        self.on_unsolicited(data)
                    continue
                if control & 0x0F != request_seq:
                    logger.warning(
                        "dnp3 response sequence mismatch",
                        extra={"expected": request_seq, "got": control & 0x0F},
                    )
                    continue
                fragments.append(data)
                if control & APP_FIN:
                    return fragments
                deadline = loop.time() + self._response_timeout_s
