"""`opengrid.fleet.device_info` (H4 safety fix): a device report never changes the seed ratings or location;
the hub comes from the topic; mismatches raise ALR-DEVICE-RATING-MISMATCH; every report is traced."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import pytest

from opengrid.core.models.platform import Alert
from opengrid.fleet import device_info
from opengrid.platform.mqtt import SchemaValidationError

SEED = {"units": 1, "p_kw": 11.0, "e_kwh": 39.2, "r_kwh": 7.84, "lat": 30.27, "lon": -97.74}
TOPIC = "og/v1/hub/hub-00012/info"


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
    def __init__(self, row: tuple[Any, ...] | None, log: list[tuple[str, dict[str, Any]]]) -> None:
        self._row = row
        self._log = log

    async def execute(self, sql: str, params: dict[str, Any]) -> None:
        self._log.append((sql, params))

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self._row

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Conn:
    def __init__(self, pool: _Pool) -> None:
        self._pool = pool

    def cursor(self) -> _Cursor:
        return _Cursor(self._pool.row, self._pool.executed)

    async def commit(self) -> None:
        self._pool.commits += 1

    async def __aenter__(self) -> _Conn:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Pool:
    def __init__(self, seed: dict[str, Any] | None) -> None:
        self.row = tuple(seed[k] for k in device_info._SEED_COLUMNS) if seed else None
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.commits = 0

    def connection(self) -> _Conn:
        return _Conn(self)


class _Trace:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, dict[str, Any]]] = []

    async def append(self, stream: str, decision_type: str, event_class: str, payload: dict[str, Any]) -> Any:
        self.events.append((stream, decision_type, event_class, payload))


class _Alerts:
    def __init__(self) -> None:
        self.open: list[Alert] = []
        self.raised: list[Any] = []
        self.cleared: list[int] = []

    async def fetch_open_alerts(self, pool: object) -> list[Alert]:
        return list(self.open)

    async def raise_alert(self, pool: object, finding: Any, *, opened_at: datetime) -> int:
        self.raised.append(finding)
        alert_id = len(self.raised)
        self.open.append(
            Alert(
                id=alert_id,
                rule=finding.rule,
                severity=finding.severity,
                summary="s",
                opened_at=opened_at,
                scope_kind=finding.detail["scope_kind"],
                scope_ref=finding.detail["scope_ref"],
            )
        )
        return alert_id

    async def clear_alert(self, pool: object, alert_id: int, *, cleared_at: datetime | None = None) -> None:
        self.cleared.append(alert_id)
        self.open = [a for a in self.open if a.id != alert_id]


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> _Alerts:
    fake = _Alerts()
    monkeypatch.setattr(device_info, "fetch_open_alerts", fake.fetch_open_alerts)
    monkeypatch.setattr(device_info, "raise_alert", fake.raise_alert)
    monkeypatch.setattr(device_info, "clear_alert", fake.clear_alert)
    return fake


SAFETY_COLUMNS = ("units", "p_kw", "e_kwh", "r_kwh", "lat", "lon")


def _writes(pool: _Pool) -> list[str]:
    return [sql for sql, _ in pool.executed if sql.lstrip().startswith("UPDATE")]


def _never_touches_safety_columns(pool: _Pool) -> None:
    for sql in _writes(pool):
        assignments = re.split(r"\bSET\b", sql, maxsplit=1)[1].split("WHERE", 1)[0]
        assigned = {part.split("=")[0].strip() for part in assignments.split(",")}
        assert not assigned & set(SAFETY_COLUMNS), assigned


async def test_a_matching_report_records_identity_and_reported_values_only(alerts: _Alerts) -> None:
    pool, trace = _Pool(SEED), _Trace()
    result = await device_info.upsert_device_info(pool, _msg(), topic=TOPIC, trace=trace)  # type: ignore[arg-type]

    assert result.accepted and result.mismatches == {}
    _never_touches_safety_columns(pool)
    _sql, params = next(e for e in pool.executed if e[0].lstrip().startswith("UPDATE"))
    assert params["serial_number"] == "BP-LZ_NORTH-00012" and params["device_reserve_floor_pct"] == 20.0
    assert [e[2] for e in trace.events] == ["DEVICE_INFO"]  # always traced
    assert alerts.raised == []


async def test_a_zero_reserve_report_never_touches_the_reserve_and_raises_an_alert(alerts: _Alerts) -> None:
    """The H4 attack: reserve_floor_pct=0 used to set r_kwh=0 and remove the K1 reserve."""
    pool, trace = _Pool(SEED), _Trace()
    result = await device_info.upsert_device_info(
        pool,
        _msg(reserve_floor_pct=0.0, units=2, rated_kw=20.0),
        topic=TOPIC,
        trace=trace,  # type: ignore[arg-type]
    )

    assert result.accepted
    assert set(result.mismatches) == {"r_kwh", "units", "p_kw"}
    _never_touches_safety_columns(pool)
    ((_s, _d, event_class, payload),) = trace.events
    assert event_class == "DEVICE_INFO" and payload["mismatches"]["r_kwh"]["reported"] == 0.0
    (finding,) = alerts.raised
    assert finding.rule == "ALR-DEVICE-RATING-MISMATCH" and finding.detail["scope_ref"] == "hub-00012"

    # raised once per hub while it persists, cleared by the first matching report
    await device_info.upsert_device_info(pool, _msg(reserve_floor_pct=0.0), topic=TOPIC, trace=trace)  # type: ignore[arg-type]
    assert len(alerts.raised) == 1
    await device_info.upsert_device_info(pool, _msg(), topic=TOPIC, trace=trace)  # type: ignore[arg-type]
    assert alerts.open == [] and alerts.cleared == [1]


async def test_a_location_change_is_a_mismatch_not_a_move(alerts: _Alerts) -> None:
    pool, trace = _Pool(SEED), _Trace()
    result = await device_info.upsert_device_info(pool, _msg(lat=29.0), topic=TOPIC, trace=trace)  # type: ignore[arg-type]
    assert set(result.mismatches) == {"lat"}
    _never_touches_safety_columns(pool)


@pytest.mark.parametrize(
    "topic", ["og/v1/hub/hub-00099/info", "og/v1/hub/info", "og/v1/tel/hub-00012", "hub-00012"]
)
async def test_a_payload_for_another_hub_or_a_bad_topic_is_rejected_and_traced(
    alerts: _Alerts, topic: str
) -> None:
    pool, trace = _Pool(SEED), _Trace()
    result = await device_info.upsert_device_info(pool, _msg(), topic=topic, trace=trace)  # type: ignore[arg-type]

    assert not result.accepted
    assert pool.executed == []  # nothing read or written
    ((_s, _d, event_class, payload),) = trace.events
    assert event_class == "DEVICE_INFO_REJECTED" and payload["reason"] in ("topic_hub_mismatch", "bad_topic")


async def test_an_unknown_hub_is_rejected_traced_and_not_created(alerts: _Alerts) -> None:
    pool, trace = _Pool(None), _Trace()
    topic = "og/v1/hub/hub-99999/info"
    result = await device_info.upsert_device_info(pool, _msg(hub_id="hub-99999"), topic=topic, trace=trace)  # type: ignore[arg-type]
    assert not result.accepted and result.reason == "unknown_hub"
    assert _writes(pool) == []
    assert [e[2] for e in trace.events] == ["DEVICE_INFO_REJECTED"]


def test_hub_id_from_topic() -> None:
    assert device_info.hub_id_from_topic("og/v1/hub/hub-00012/info") == "hub-00012"
    assert device_info.hub_id_from_topic("ogtest/fups/og/v1/hub/h/info") == "h"
    assert device_info.hub_id_from_topic("og/v1/hub/hub-00012/tel") is None


@pytest.mark.parametrize(
    "bad",
    [
        {"units": 3},
        {"rated_kw": 0},
        {"reserve_floor_pct": 120},
        {"asset_class": "EV"},
        {"install_date": "14/03/2025"},
        {"extra_field": 1},
        # R4: control characters (a NUL poisons jsonb and the trace journal) and unbounded strings
        {"manufacturer": "evil\x00corp"},
        {"model": "line\nbreak"},
        {"serial_number": "x" * 129},
        {"firmware_version": "\x7f"},
        {"hub_id": "hub/00012"},
    ],
)
async def test_invalid_messages_are_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(SchemaValidationError):
        await device_info.upsert_device_info(_Pool(SEED), _msg(**bad), topic=TOPIC, trace=_Trace())  # type: ignore[arg-type]


def test_the_update_statement_never_names_a_safety_column() -> None:
    assignments = device_info._UPDATE_REPORTED_SQL.split(" SET", 1)[1].split("WHERE", 1)[0]
    assigned = {part.split("=")[0].strip() for part in assignments.split(",")}
    assert not assigned & set(SAFETY_COLUMNS)


def test_now_is_utc_aware() -> None:
    assert datetime.now(UTC).tzinfo is UTC
