"""`opengrid.safestop.engage`/`release` delegate to whatever `SafestopService` `configure_service()`
installed (INTERFACES.md fixes the free-function signatures; this is how they stay wired to a real or
fake backend without changing that signature)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

import opengrid.safestop as safestop


@dataclass
class _FakeService:
    engaged: list[tuple[str, str, str, str]] = field(default_factory=list)
    released: list[tuple[str, str, str]] = field(default_factory=list)

    async def engage(self, scope: str, scope_ref: str, reason: str, initiator_ref: str) -> None:
        self.engaged.append((scope, scope_ref, reason, initiator_ref))

    async def release(self, scope: str, scope_ref: str, approver_ref: str) -> None:
        self.released.append((scope, scope_ref, approver_ref))
        raise safestop.ReleaseNotPermittedError("no")


@pytest.fixture(autouse=True)
def _reset_service():
    safestop.configure_service(None)
    yield
    safestop.configure_service(None)


async def test_engage_without_configure_raises():
    with pytest.raises(Exception, match="configure_service"):
        await safestop.engage("BANK", "bank-07", "reason", "operator:alice")


async def test_engage_delegates_to_configured_service():
    fake: Any = _FakeService()
    safestop.configure_service(fake)  # type: ignore[arg-type]

    await safestop.engage("ZONE", "LZ_NORTH", "drill", "operator:bob")

    assert fake.engaged == [("ZONE", "LZ_NORTH", "drill", "operator:bob")]


async def test_release_delegates_and_still_raises():
    fake: Any = _FakeService()
    safestop.configure_service(fake)  # type: ignore[arg-type]

    with pytest.raises(safestop.ReleaseNotPermittedError):
        await safestop.release("FLEET", "", "operator:carol")

    assert fake.released == [("FLEET", "", "operator:carol")]
