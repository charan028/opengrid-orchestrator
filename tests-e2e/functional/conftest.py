"""Shared fixtures for the functional suites (Q1 `dispatch/`, Q2 `safety/`), run against the dev stack:

    make -C dev dev-up-full          # or: docker compose -f dev/docker-compose.yml --profile orchestrator up -d
    pytest tests-e2e/functional -v

Every test is skipped, not failed, when the stack is not running.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_stack import Stack


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "slow: waits for a real delivery window (up to ~20 min)")


@pytest.fixture(scope="session")
def stack() -> Iterator[Stack]:
    client = Stack()
    why_not = client.reachable()
    if why_not is not None:
        pytest.skip(f"dev stack not available: {why_not}")
    yield client
    client.cleanup()
