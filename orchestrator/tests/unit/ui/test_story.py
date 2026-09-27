"""The Story screen (`/og/story`): the narrative is derived from the same reads the operational screens
make, and the derivation is pinned exactly here so a number on the page can never drift from its source."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

from opengrid.ui.routes.story import LIVE_STATES, SERVICE_LABELS, STAGES, story_page_view

from .conftest import load_fixture

VIEWER = {"X-Remote-User": "carol"}


def _obligation(**over: Any) -> dict[str, Any]:
    base = {
        "obligation_id": "o",
        "contract_id": "c",
        "service_type": "ERCOT_ENERGY",
        "committed_qty_kw": 10.0,
        "state": "COMMITTED",
        "at_risk": False,
    }
    return {**base, **over}


def test_view_counts_only_live_obligations_and_groups_them_by_buyer_type() -> None:
    obligations = [
        _obligation(obligation_id="a", contract_id="c1", committed_qty_kw=10.0),
        _obligation(obligation_id="b", contract_id="c1", committed_qty_kw=5.5, state="DELIVERING"),
        _obligation(
            obligation_id="c", contract_id="c2", service_type="ERCOT_AS", committed_qty_kw=20.0, at_risk=True
        ),
        _obligation(obligation_id="d", contract_id="c3", state="OFFERED", committed_qty_kw=999.0),
        _obligation(obligation_id="e", contract_id="c4", state="EXPIRED", committed_qty_kw=999.0),
    ]
    view = story_page_view({}, obligations, {}, [], [])

    assert view["live_obligations"] == 3
    assert view["distinct_buyers"] == 2
    assert view["promised_kw"] == 35.5
    assert [row["service_type"] for row in view["buyers"]] == ["ERCOT_AS", "ERCOT_ENERGY"]  # richest first
    energy = view["buyers"][1]
    assert (energy["kw"], energy["count"], energy["buyers"], energy["delivering"], energy["at_risk"]) == (
        15.5,
        2,
        1,
        1,
        0,
    )
    assert view["buyers"][0]["at_risk"] == 1
    assert view["catalogue_live_count"] == 2
    assert {c["service_type"] for c in view["catalogue"]} == set(SERVICE_LABELS)
    assert all(state in LIVE_STATES for state in ("SELECTED", "COMMITTED", "DELIVERING"))


def test_view_money_is_summed_from_settlement_rows_and_baseline_only_where_present() -> None:
    settlement = {
        "as_of": "2026-09-26T19:00:00+00:00",
        "period": {"from": "2026-08-27"},
        "pnl_rows": [
            {"net_value": "10.5", "rule_baseline_value": "4.0", "forgone_upside": "1.25"},
            {"net_value": "-2.0", "rule_baseline_value": None, "forgone_upside": "0"},
            {"net_value": "3.0", "rule_baseline_value": "3.0", "forgone_upside": "0.75"},
        ],
    }
    view = story_page_view({}, [], settlement, [], [])

    assert view["net_value"] == 11.5
    assert view["forgone_upside"] == 2.0
    assert view["value_of_orchestration"] == 6.5  # rows 1 and 3 only; the row without a baseline is skipped
    assert view["pnl_rows"] == 3
    assert view["period"]["from"] == "2026-08-27"
    assert view["settlement_as_of"] == "2026-09-26T19:00:00+00:00"


def test_view_without_a_baseline_says_so_instead_of_inventing_a_number() -> None:
    view = story_page_view({}, [], {"pnl_rows": [{"net_value": "1", "forgone_upside": "0"}]}, [], [])
    assert view["value_of_orchestration"] is None


def test_view_promises_and_pipeline_come_from_health() -> None:
    health = load_fixture("health.json")
    view = story_page_view(health, [], {}, [], [])

    assert view["promises"] == {"reserve_breaches": 0, "double_sold_kwh": 0, "commitment_switches": 0}
    assert view["promises_kept"] is True
    assert view["hubs_online"] == int((health.get("hub_health_counts") or {}).get("online") or 0)
    assert [s["key"] for s in view["stages"]] == [s["key"] for s in STAGES]
    assert view["stages_ok"] == sum(1 for s in view["stages"] if s["status"] == "ok")
    assert view["invariants_as_of"] == health["as_of"]  # no separate invariants stamp in this fixture

    broken = story_page_view({**health, "reserve_breaches": 1}, [], {}, [], [])
    assert broken["promises_kept"] is False


def test_view_accepts_unit_named_processes_from_an_older_api() -> None:
    health = {"processes": {"og-guardian": {"status": "down"}, "engine": {"status": "ok"}}}
    stages = {s["key"]: s["status"] for s in story_page_view(health, [], {}, [], [])["stages"]}
    assert stages["guardian"] == "down"
    assert stages["engine"] == "ok"
    assert stages["feeds"] == "unknown"


def test_view_zones_come_from_the_hub_list() -> None:
    hubs = [{"zone": "LZ_SOUTH"}, {"zone": "LZ_NORTH"}, {"zone": "LZ_SOUTH"}, {"zone": None}]
    assert story_page_view({}, [], {}, hubs, [])["zones"] == ["LZ_NORTH", "LZ_SOUTH"]


def _install_story_fixtures(fake_api: Callable[[dict[str, Any]], None], monkeypatch: Any) -> None:
    import opengrid.ui.api_client as api_client
    import opengrid.ui.routes.story as story

    responses = {
        "/og/api/health": load_fixture("health.json"),
        "/og/api/dispatch/opportunities": load_fixture("dispatch_obligations.json"),
        "/og/api/views/settlement": load_fixture("views_settlement.json"),
        "/og/api/fleet/hubs": load_fixture("hubs.json"),
        "/og/api/markets/series": load_fixture("markets_series_price.json"),
    }
    fake_api(responses)
    # `fake_api` patches the modules it knows about; this screen imported `get_json` by name too.
    monkeypatch.setattr(story, "get_json", api_client.get_json)


def test_story_screen_renders_the_narrative_over_live_data(
    client: TestClient, fake_api: Callable[[dict[str, Any]], None], monkeypatch: Any
) -> None:
    _install_story_fixtures(fake_api, monkeypatch)

    resp = client.get("/og/story", headers=VIEWER)
    assert resp.status_code == 200
    body = resp.text
    assert body.count("<h1") == 1
    assert "One fleet, many buyers, homes first" in body
    assert "sells each kilowatt-hour" in body
    # the three promises are live tiles the stream can update, each with an age
    for tile in ("story-reserve-breaches", "story-double-sold", "story-commitment-switches"):
        assert f'id="{tile}"' in body
    assert body.count("stale-badge") >= 8
    # pipeline stages carry the process heartbeat as data
    assert 'data-status="ok"' in body
    assert "og-guardian: ok" in body
    # the stream handler reads the real payload field names
    assert "data.reserve_breach_count" in body
    assert "/api/stream/control-room" in body
    # nothing in the narrative is a control (the copilot form lives in the nav, outside the article)
    article = body.split('<article class="st">', 1)[1].split("</article>", 1)[0]
    assert "<form" not in article and "<button" not in article
    assert 'aria-current="page"' in body.split('href="/og/story"', 1)[1].split("</a>", 1)[0]


def test_story_screen_degrades_honestly_when_the_api_is_down(client: TestClient) -> None:
    # no fixtures registered -> every read raises ApiUnavailable
    resp = client.get("/og/story", headers=VIEWER)
    assert resp.status_code == 200
    assert "Degraded:" in resp.text
    assert "No obligation is live at this moment" in resp.text
    assert "No interval has settled yet" in resp.text
