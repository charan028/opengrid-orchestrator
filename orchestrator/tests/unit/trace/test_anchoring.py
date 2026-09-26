"""Unit tests for `opengrid.trace.anchoring` (K11 external anchoring, adversarial review): publishing
writes a signed primary anchor file plus an independent secondary copy, degrades to unsigned rather than
blocking when no key is configured, records the publish in `og.trace_anchor`, and stamps the matching
`og.trace_checkpoint.anchor_ref`. No real Postgres or filesystem beyond pytest's own `tmp_path`."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from opengrid.core.crypto import generate_keypair, verify_payload
from opengrid.platform.config import Config
from opengrid.trace import anchoring


class _FakeCursor:
    def __init__(self, db: _FakeDb) -> None:
        self._db = db
        self._result: tuple | None = None

    async def execute(self, query: str, params=None) -> None:
        params = params or {}
        if "SELECT checkpoint_id FROM og.trace_checkpoint" in query:
            row = self._db.checkpoints.get(params["checkpoint_hash"])
            self._result = (row,) if row is not None else None
        elif "UPDATE og.trace_checkpoint SET anchor_ref" in query:
            self._db.anchor_refs[params["checkpoint_id"]] = params["anchor_ref"]
        elif "INSERT INTO og.trace_anchor" in query:
            self._db.anchors.append(dict(params))
        else:  # pragma: no cover
            raise AssertionError(f"unexpected query: {query!r}")

    async def fetchone(self):
        return self._result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, db: _FakeDb) -> None:
        self._db = db

    def cursor(self):
        return _FakeCursor(self._db)

    async def commit(self) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeDb:
    def __init__(self) -> None:
        self.checkpoints: dict[str, object] = {}
        self.anchor_refs: dict[object, str] = {}
        self.anchors: list[dict] = []

    def connection(self):
        return _FakeConn(self)


class _FakeTraceStore:
    def __init__(self, checkpoint_hash: str) -> None:
        self._checkpoint_hash = checkpoint_hash

    async def checkpoint(self) -> str:
        return self._checkpoint_hash


def _cfg(**overrides: object) -> Config:
    return Config(_nested(overrides))


def _nested(flat: dict[str, object]) -> dict[str, object]:
    data: dict[str, object] = {}
    for dotted, value in flat.items():
        node = data
        parts = dotted.split("__")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return data


@pytest.mark.asyncio
async def test_publish_anchor_writes_primary_and_secondary_files_unsigned(tmp_path: Path) -> None:
    pool = _FakeDb()
    store = _FakeTraceStore("deadbeef")
    cfg = _cfg(
        trace__anchor_dir=str(tmp_path / "primary"), trace__anchor_secondary_dir=str(tmp_path / "secondary")
    )

    result = await anchoring.publish_anchor(pool, store, cfg)

    assert result.signed is False
    primary = json.loads(Path(result.primary_path).read_text())  # noqa: ASYNC240
    secondary = json.loads(Path(result.secondary_path).read_text())  # noqa: ASYNC240
    assert primary == secondary
    assert primary["checkpoint_hash"] == "deadbeef"
    assert primary["signature"] is None
    assert len(pool.anchors) == 1
    assert pool.anchors[0]["checkpoint_hash"] == "deadbeef"


@pytest.mark.asyncio
async def test_publish_anchor_signs_when_key_configured(tmp_path: Path) -> None:
    seed, pub = generate_keypair()
    key_path = tmp_path / "anchor.key"
    key_path.write_bytes(seed)
    pool = _FakeDb()
    store = _FakeTraceStore("cafef00d")
    cfg = _cfg(
        trace__anchor_dir=str(tmp_path / "primary"),
        trace__anchor_secondary_dir=str(tmp_path / "secondary"),
        trace__anchor_key_path=str(key_path),
        trace__anchor_key_id="anchor-1",
    )

    result = await anchoring.publish_anchor(pool, store, cfg)

    assert result.signed is True
    document = json.loads(Path(result.primary_path).read_text())  # noqa: ASYNC240
    signature = document.pop("signature")
    assert verify_payload(pub, document, signature)
    assert pool.anchors[0]["key_id"] == "anchor-1"


@pytest.mark.asyncio
async def test_publish_anchor_stamps_matching_checkpoint_anchor_ref(tmp_path: Path) -> None:
    pool = _FakeDb()
    checkpoint_id = uuid4()
    pool.checkpoints["abc123"] = checkpoint_id
    store = _FakeTraceStore("abc123")
    cfg = _cfg(
        trace__anchor_dir=str(tmp_path / "primary"), trace__anchor_secondary_dir=str(tmp_path / "secondary")
    )

    result = await anchoring.publish_anchor(pool, store, cfg)

    assert result.checkpoint_id == checkpoint_id
    assert pool.anchor_refs[checkpoint_id] == result.primary_path


@pytest.mark.asyncio
async def test_publish_anchor_survives_secondary_copy_failure(tmp_path: Path, monkeypatch) -> None:
    pool = _FakeDb()
    store = _FakeTraceStore("11223344")
    cfg = _cfg(
        trace__anchor_dir=str(tmp_path / "primary"), trace__anchor_secondary_dir=str(tmp_path / "secondary")
    )

    real_write = anchoring._write_anchor_file
    calls = {"n": 0}

    def _flaky_write(directory, anchor_id, document):
        calls["n"] += 1
        if calls["n"] == 2:  # the secondary write
            raise OSError("simulated disk failure")
        return real_write(directory, anchor_id, document)

    monkeypatch.setattr(anchoring, "_write_anchor_file", _flaky_write)

    result = await anchoring.publish_anchor(pool, store, cfg)

    assert result.secondary_path is None
    assert Path(result.primary_path).exists()  # noqa: ASYNC240
    assert len(pool.anchors) == 1  # the DB record still gets written


def test_load_anchor_key_degrades_when_file_unreadable(tmp_path: Path) -> None:
    cfg = _cfg(trace__anchor_key_path=str(tmp_path / "missing.key"), trace__anchor_key_id="k1")

    key = anchoring.load_anchor_key(cfg)

    assert key.seed is None
    assert key.key_id == "k1"


def test_load_anchor_key_defaults_to_unsigned_when_unconfigured() -> None:
    key = anchoring.load_anchor_key(Config({}))

    assert key.seed is None
    assert key.key_id == "anchor-default"
