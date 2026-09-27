"""Fixtures for the grid-link tests."""

from __future__ import annotations

import pytest

from .fakes import Harness, make_harness


@pytest.fixture
def harness() -> Harness:
    return make_harness()
