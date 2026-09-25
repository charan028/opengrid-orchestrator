"""FastAPI dependency providers: everything routers need comes off `request.app.state`, set up once
in `app.py`'s lifespan (02b S7). Kept as thin functions so tests can override each one independently
(`app.dependency_overrides[get_store] = lambda: FakeStore(...)`).
"""

from __future__ import annotations

from fastapi import Request
from psycopg_pool import AsyncConnectionPool

from opengrid.api.proposals import ProposalStore
from opengrid.api.store import StoreProtocol
from opengrid.platform.config import Config
from opengrid.trace.store import TraceStore


def get_config(request: Request) -> Config:
    config: Config = request.app.state.config
    return config


def get_pool(request: Request) -> AsyncConnectionPool:
    pool: AsyncConnectionPool = request.app.state.pool
    return pool


def get_store(request: Request) -> StoreProtocol:
    store: StoreProtocol = request.app.state.store
    return store


def get_trace_store(request: Request) -> TraceStore:
    trace_store: TraceStore = request.app.state.trace_store
    return trace_store


def get_proposals(request: Request) -> ProposalStore:
    proposals: ProposalStore = request.app.state.proposals
    return proposals
