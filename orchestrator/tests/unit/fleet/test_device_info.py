"""`opengrid.fleet.device_info`: schema validation, identity recorded, ratings changed only when they
differ from the seed (and traced first), unknown hubs never created."""

from __future__ import annotations

from typing import Any

import pytest

from opengrid.fleet import device_info
from opengrid.platform.mqtt import SchemaValidationError

SEED = {"units": 1, "p_kw": 11.0, "e_kwh": 39.2, "r_kwh": 7.84, "lat": 30.27, "lon": -97.74}


def _msg(**overrides: Any) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "hub_id": "hub-00012",
        "serial_number": "BP-LZ_NORTH-00012",
        "manufacturer": "Base Power",
        "model": "BP-1",
        "firmware_version": "3.4.1",
        "hardware_revision": "C",
        "install_date": "2025-03-14",
        "commissioning_date": "2025-03-21",
        "asset_class": "HOME_BESS",
        "units": 1,
        "rated_kw": 11.0,
        "rated_kwh": 39.2,
        "reserve_floor_pct": 20.0,
        "lat": 30.27,
        "lon": -97.74,
        "inverter_model": "INV-11",
        "ts": "2026-09-26T18:00:00Z",
    }
    msg.update(overrides)
    return msg


class _Cursor:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row
        self.executed: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, sql: str, params: dict[str, Any]) -> None:
        self.executed.append((sql, params))

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self._row

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Conn:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor
        self.committed = False
        self.rolled_back = False

    def cursor(self) -> _Cursor:
        return self._cursor

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def __aenter__(self) -> _Conn:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Pool:
    def __init__(self, row: dict[str, Any] | None) -> None:
        values = tuple(row[k] for k in ("units", "p_kw", "e_kwh", "r_kwh", "lat", "lon")) if row else None
        self.cursor = _Cursor(values)
        self.conn = _Conn(self.cursor)

    def connection(self) -> _Conn:
        return self.conn


class _Trace:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, dict[str, Any]]] = []

    async def append(self, stream: str, decision_type: str, event_class: str, payload: dict[str, Any]) -> Any:
        self.events.append((stream, decision_type, event_class, payload))


async def test_identity_is_recorded_and_matching_ratings_are_left_alone() -> None:
    pool, trace = _Pool(SEED), _Trace()
    result = await device_info.upsert_device_info(pool, _msg(), trace=trace)  # type: ignore[arg-type]

    assert result.found and result.rating_changes == {}
    assert trace.events == []  # nothing re-rated, nothing traced
    sqls = [sql for sql, _ in pool.cursor.executed]
    assert not any(sql.startswith("UPDATE og.hub SET units") for sql in sqls)
    (_sql, identity) = pool.cursor.executed[-1]
    assert identity["serial_number"] == "BP-LZ_NORTH-00012" and identity["firmware_version"] == "3.4.1"
    assert str(identity["installed_at"]) == "2025-03-14" and str(identity["commissioned_at"]) == "2025-03-21"
    assert identity["device_info_at"].isoformat() == "2026-09-26T18:00:00+00:00"
    assert pool.conn.committed


async def test_a_differing_rating_is_traced_then_applied() -> None:
    pool, trace = _Pool(SEED), _Trace()
    msg = _msg(units=2, rated_kw=20.0, rated_kwh=78.4, reserve_floor_pct=20.0)
    result = await device_info.upsert_device_info(pool, msg, trace=trace)  # type: ignore[arg-type]

    assert set(result.rating_changes) == {"units", "p_kw", "e_kwh", "r_kwh"}
    assert result.rating_changes["r_kwh"]["new"] == pytest.approx(15.68)
    ((stream, decision_type, event_class, payload),) = trace.events
    assert (stream, decision_type, event_class) == (
        "fleet_device_info",
        "ASSET_STATE_TRANSITION",
        "FLEET_CHANGE",
    )
    assert payload["changes"]["units"] == {"old": 1, "new": 2}
    applied = {
        sql.split(" SET ")[1].split(" =")[0]: p["value"] for sql, p in pool.cursor.executed if "value" in p
    }
    assert applied["units"] == 2 and applied["p_kw"] == 20.0


async def test_an_unknown_hub_is_not_created() -> None:
    pool = _Pool(None)
    result = await device_info.upsert_device_info(pool, _msg(hub_id="hub-99999"))  # type: ignore[arg-type]
    assert not result.found
    assert pool.conn.rolled_back and not pool.conn.committed
    assert len(pool.cursor.executed) == 1  # only the lookup


@pytest.mark.parametrize(
    "bad",
    [
        {"units": 3},
        {"rated_kw": 0},
        {"reserve_floor_pct": 120},
        {"asset_class": "EV"},
        {"install_date": "14/03/2025"},
        {"extra_field": 1},
    ],
)
async def test_invalid_messages_are_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(SchemaValidationError):
        await device_info.upsert_device_info(_Pool(SEED), _msg(**bad))  # type: ignore[arg-type]


def test_missing_required_field_is_rejected() -> None:
    msg = _msg()
    del msg["serial_number"]
    with pytest.raises(SchemaValidationError):
        device_info.validate_device_info(msg)
