"""Dev-only stand-in for Apache in front of og-api: injects `X-OG-Proxy-Auth`, streams responses.

og-api believes `X-Remote-User` only alongside `X-OG-Proxy-Auth` = `OG_API_PROXY_SECRET`
(`opengrid.api.auth.proxy_authenticated`). In production Apache sets both after Basic Auth; the dev
stack has no Apache, so a browser pointed straight at og-api is refused with 401. This proxy fills that
gap for local runs such as `tests-e2e/ui` in live mode (`OG_UI_BASE_URL`):

- listens on 127.0.0.1 only (default port 8088) and forwards to og-api (default http://127.0.0.1:8080);
- drops any client-supplied `X-OG-Proxy-Auth` and sets its own from `dev/secrets`, never printing it;
- passes the client's `X-Remote-User` through, which stands in for the Basic-Auth user Apache would
  set. Any local process can therefore pick an identity through this proxy: dev stack only, never
  deploy it;
- streams response bodies, so SSE (`/og/api/stream/*`) works.

Run from the repository root with the orchestrator's venv:

    python dev/scripts/dev_proxy.py [--port 8088] [--upstream http://127.0.0.1:8080]
"""

from __future__ import annotations

import argparse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import PlainTextResponse, StreamingResponse
from starlette.routing import Route

SECRETS_FILE = Path(__file__).resolve().parents[1] / "secrets"
SECRET_KEY = "OG_API_PROXY_SECRET"
PROXY_AUTH_HEADER = "x-og-proxy-auth"
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
    }
)


def load_proxy_secret(path: Path = SECRETS_FILE) -> str:
    """The `OG_API_PROXY_SECRET` value from dev/secrets. Raises without echoing any file content."""
    if not path.is_file():
        raise SystemExit(
            f"dev_proxy: {path} not found (create it from dev/secrets.example)"
        )
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == SECRET_KEY and value.strip():
            return value.strip()
    raise SystemExit(f"dev_proxy: {SECRET_KEY} is missing or empty in {path}")


def build_app(upstream: str, secret: str) -> Starlette:
    client_holder: dict[str, httpx.AsyncClient] = {}

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        # read=None: SSE streams stay open indefinitely
        timeout = httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0)
        async with httpx.AsyncClient(base_url=upstream, timeout=timeout) as client:
            client_holder["client"] = client
            yield

    async def forward(request: Request) -> StreamingResponse | PlainTextResponse:
        client = client_holder["client"]
        headers = [
            (k, v)
            for k, v in request.headers.items()
            if k.lower() not in HOP_BY_HOP and k.lower() != PROXY_AUTH_HEADER
        ]
        headers.append((PROXY_AUTH_HEADER, secret))
        upstream_request = client.build_request(
            request.method,
            request.url.path,
            params=request.url.query or None,
            headers=headers,
            content=await request.body(),
        )
        try:
            upstream_response = await client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            return PlainTextResponse(
                f"dev_proxy: upstream unreachable ({type(exc).__name__})",
                status_code=502,
            )
        response = StreamingResponse(
            upstream_response.aiter_raw(),
            status_code=upstream_response.status_code,
            background=BackgroundTask(upstream_response.aclose),
        )
        # multi_items keeps repeated headers such as Set-Cookie
        response.raw_headers = [
            (k.encode("latin-1"), v.encode("latin-1"))
            for k, v in upstream_response.headers.multi_items()
            if k.lower() not in HOP_BY_HOP
        ]
        return response

    methods = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    return Starlette(
        routes=[Route("/{path:path}", forward, methods=methods)], lifespan=lifespan
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--upstream", default="http://127.0.0.1:8080")
    args = parser.parse_args()
    app = build_app(args.upstream.rstrip("/"), load_proxy_secret())
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
