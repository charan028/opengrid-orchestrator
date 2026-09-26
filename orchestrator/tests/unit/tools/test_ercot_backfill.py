"""tools/ercot_backfill.py against a fake ERCOT HTTP server (`httpx.MockTransport`) driving the real
`ErcotClient` + normalizers, and an in-memory insert-only sink with the live ingest's natural key."""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from opengrid.core.models.platform import FeedObs
from opengrid.feeds.ercot import ErcotClient

_TOOL = Path(__file__).resolve().parents[3] / "tools" / "ercot_backfill.py"


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ercot_backfill", _TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ercot_backfill"] = module
    spec.loader.exec_module(module)
    return module


bf = _load_tool()

_SPP_FIELDS = [
    {"name": n}
    for n in (
        "deliveryDate",
        "deliveryHour",
        "deliveryInterval",
        "settlementPoint",
        "settlementPointType",
        "settlementPointPrice",
        "DSTFlag",
    )
]
_LOAD_FIELDS = [
    {"name": n}
    for n in (
        "operatingDay",
        "hourEnding",
        "coast",
        "east",
        "farWest",
        "north",
        "northC",
        "southern",
        "southC",
        "west",
        "total",
        "DSTFlag",
    )
]


def _spp_payload(day: str, point_type: str, page: int, total_pages: int) -> dict[str, object]:
    points = {"LZ": ["LZ_NORTH", "LZ_SOUTH"], "HU": ["HB_NORTH", "XYZ_RN1"], "AH": ["HB_HUBAVG"]}[point_type]
    rows = [[day, page, 1, sp, point_type, 25.0 + page, False] for sp in points]
    return {"_meta": {"totalPages": total_pages, "currentPage": page}, "fields": _SPP_FIELDS, "data": rows}


def _load_payload(day: str) -> dict[str, object]:
    rows = [[day, f"{h:02d}:00", *([1000.0] * 9), False] for h in (1, 2)]
    return {"_meta": {"totalPages": 1}, "fields": _LOAD_FIELDS, "data": rows}


class FakeErcot:
    """Fake ERCOT public API: token endpoint + the two backfill products, 2 pages for LZ prices."""

    def __init__(self, *, fail_on: tuple[str, str] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.fail_on = fail_on  # (settlementPointType, deliveryDateFrom) -> 429

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith("http://test/token"):
            return httpx.Response(200, json={"id_token": "tok"})
        self.requests.append(request)
        q = request.url.params
        if request.url.path.endswith("/spp_node_zone_hub"):
            day, spt, page = q["deliveryDateFrom"], q["settlementPointType"], int(q["page"])
            assert q["deliveryDateTo"] == day
            if self.fail_on == (spt, day):
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(200, json=_spp_payload(day, spt, page, 2 if spt == "LZ" else 1))
        if request.url.path.endswith("/act_sys_load_by_wzn"):
            assert q["operatingDayFrom"] == q["operatingDayTo"]
            return httpx.Response(200, json=_load_payload(q["operatingDayFrom"]))
        return httpx.Response(404)


class MemorySink:
    """Insert-only on (source, product, series, ts), like `FeedStore.insert_obs_if_absent`."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str, object], FeedObs] = {}

    async def insert_obs_if_absent(self, rows: list[FeedObs]) -> int:
        n = 0
        for r in rows:
            key = (r.source, r.product, r.series, r.ts)
            if key not in self.rows:
                self.rows[key] = r
                n += 1
        return n


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in (("U", "u"), ("P", "p"), ("K1", "k1"), ("K2", "k2")):
        monkeypatch.setenv(f"TEST_BF_{name}", value)


def _client(fake: FakeErcot) -> ErcotClient:
    return ErcotClient(
        base_url="http://test/ercot",
        username_env="TEST_BF_U",
        password_env="TEST_BF_P",
        primary_key_env="TEST_BF_K1",
        secondary_key_env="TEST_BF_K2",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake)),
        token_url="http://test/token",
    )


class CountingLimiter:
    def __init__(self) -> None:
        self.calls = 0

    async def acquire(self) -> None:
        self.calls += 1


def test_plan_units_newest_first_lz_hub_types_and_load() -> None:
    units = bf.plan_units(date(2026, 9, 10), date(2026, 9, 11))
    assert [u.key for u in units] == [
        "np6-905-cd|LZ|2026-09-11",
        "np6-905-cd|HU|2026-09-11",
        "np6-905-cd|AH|2026-09-11",
        "np6-345-cd|-|2026-09-11",
        "np6-905-cd|LZ|2026-09-10",
        "np6-905-cd|HU|2026-09-10",
        "np6-905-cd|AH|2026-09-10",
        "np6-345-cd|-|2026-09-10",
    ]


async def test_backfill_pages_filters_nodes_and_rate_limits_every_request(tmp_path: Path) -> None:
    fake, sink, limiter = FakeErcot(), MemorySink(), CountingLimiter()
    cp = bf.Checkpoint(
        path=tmp_path / "cp.json", start="2026-09-11", end="2026-09-11", products=list(bf.PRODUCTS)
    )
    units = bf.plan_units(date(2026, 9, 11), date(2026, 9, 11))

    await bf.run_backfill(units, _client(fake), sink, cp, acquire=limiter.acquire, page_size=500)

    assert len(fake.requests) == 5  # LZ x2 pages, HU, AH, load
    assert limiter.calls == len(fake.requests)
    assert {r.url.params["size"] for r in fake.requests} == {"500"}
    series = {k[2] for k in sink.rows}
    assert "XYZ_RN1" not in series  # resource nodes never stored
    assert {"LZ_NORTH", "LZ_SOUTH", "HB_NORTH", "HB_HUBAVG", "north", "total"} <= series
    assert cp.done["np6-905-cd|LZ|2026-09-11"] == {"rows": 4, "inserted": 4}
    report = bf.status_report(bf.Checkpoint.load(cp.path))
    assert report["state"] == "complete"
    assert report["products"]["np6-905-cd"]["days_loaded"] == 1
    assert report["products"]["np6-345-cd"]["rows_inserted"] == 18


async def test_existing_rows_are_left_alone_on_rerun(tmp_path: Path) -> None:
    sink = MemorySink()
    units = bf.plan_units(date(2026, 9, 11), date(2026, 9, 11))
    first = bf.Checkpoint(path=tmp_path / "a.json")
    await bf.run_backfill(units, _client(FakeErcot()), sink, first, acquire=CountingLimiter().acquire)
    before = dict(sink.rows)
    second = bf.Checkpoint(path=tmp_path / "b.json")
    await bf.run_backfill(units, _client(FakeErcot()), sink, second, acquire=CountingLimiter().acquire)
    assert sink.rows == before
    assert all(v["inserted"] == 0 for v in second.done.values())


async def test_429_stops_with_checkpoint_then_resume_skips_done_units(tmp_path: Path) -> None:
    path = tmp_path / "cp.json"
    units = bf.plan_units(date(2026, 9, 10), date(2026, 9, 11))
    sink = MemorySink()
    cp = bf.Checkpoint(path=path, start="2026-09-10", end="2026-09-11", products=list(bf.PRODUCTS))
    with pytest.raises(bf.BackfillAbortedError, match="status=429"):
        await bf.run_backfill(
            units,
            _client(FakeErcot(fail_on=("HU", "2026-09-10"))),
            sink,
            cp,
            acquire=CountingLimiter().acquire,
        )
    saved = bf.Checkpoint.load(path)
    assert "np6-905-cd|LZ|2026-09-10" in saved.done
    assert "np6-905-cd|HU|2026-09-10" not in saved.done
    assert saved.last_error is not None

    fake = FakeErcot()
    await bf.run_backfill(units, _client(fake), sink, saved, acquire=CountingLimiter().acquire)
    fetched = {
        (r.url.params.get("settlementPointType"), r.url.params.get("deliveryDateFrom")) for r in fake.requests
    }
    assert ("LZ", "2026-09-11") not in fetched  # already done before the 429: not re-requested
    assert bf.status_report(bf.Checkpoint.load(path))["state"] == "complete"


async def test_resume_mid_unit_continues_at_next_page(tmp_path: Path) -> None:
    cp = bf.Checkpoint(path=tmp_path / "cp.json")
    cp.next_page["np6-905-cd|LZ|2026-09-11"] = 2
    cp.partial["np6-905-cd|LZ|2026-09-11"] = {"rows": 2, "inserted": 2}
    fake = FakeErcot()
    unit = bf.Unit("np6-905-cd", "LZ", date(2026, 9, 11))
    await bf.run_backfill([unit], _client(fake), MemorySink(), cp, acquire=CountingLimiter().acquire)
    assert [r.url.params["page"] for r in fake.requests] == ["2"]
    assert cp.done[unit.key] == {"rows": 4, "inserted": 4}


def test_rate_above_six_per_minute_is_refused() -> None:
    with pytest.raises(SystemExit):
        bf._parse_args(["--rate-per-min", "7"])
    assert bf._parse_args(["--dry-run"]).rate_per_min == 6.0


def test_status_before_start() -> None:
    assert bf.status_report(bf.Checkpoint(path=Path("nowhere.json")))["state"] == "not started"
