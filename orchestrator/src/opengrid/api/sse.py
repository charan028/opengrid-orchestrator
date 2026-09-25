"""SSE helper: every stream in 02b S7.2 is "poll the read model on a fixed cadence, coalesce, emit."
`sse_starlette.EventSourceResponse`'s `ping` parameter already sends the `: keepalive`-style comment
heartbeat (02b S7.2 "every SSE endpoint sends a comment heartbeat every `api.sse_heartbeat_s`"), so
this module only needs to supply the data-producing generator.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import Request
from sse_starlette.sse import EventSourceResponse


async def poll_stream(
    request: Request,
    *,
    interval_s: float,
    fetch: Callable[[], Awaitable[Any]],
) -> AsyncIterator[dict[str, str]]:
    """Call `fetch()` every `interval_s` seconds and emit its JSON-encoded result, until the client
    disconnects. `fetch` returning `None` means "no change" -- nothing is emitted that cycle."""
    while not await request.is_disconnected():
        payload = await fetch()
        if payload is not None:
            yield {"event": "message", "data": json.dumps(payload, default=str)}
        await asyncio.sleep(interval_s)


def sse_response(
    request: Request,
    *,
    interval_s: float,
    heartbeat_s: float,
    fetch: Callable[[], Awaitable[Any]],
) -> EventSourceResponse:
    return EventSourceResponse(poll_stream(request, interval_s=interval_s, fetch=fetch), ping=heartbeat_s)
