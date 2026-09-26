"""Fleet at scale (owner review R3): paging, filters and URL state, typeahead, select page / all matching,
the hub drawer, action-card alignment, viewer read-only and 390 px. Fixture mode serves 130 synthetic
hubs through `fleet_fixture` (params-aware)."""

from __future__ import annotations

import os

import pytest
from playwright.sync_api import Page, expect

from conftest import live_base_url
from screens import BASE_PATH, goto_ok

pytestmark = pytest.mark.skipif(bool(live_base_url()), reason="asserts the fixture fleet's exact counts")


def _shot(page: Page, name: str) -> None:
    shots = os.environ.get("OG_UI_SCREENSHOT_DIR")
    if shots:
        page.screenshot(path=os.path.join(shots, name), full_page=True)


def test_pages_by_cursor_and_shows_the_approximate_total(operator_page: Page) -> None:
    operator_page.set_viewport_size({"width": 1400, "height": 1000})
    goto_ok(operator_page, f"{BASE_PATH}/fleet")
    rows = operator_page.locator("#fleet-table tbody tr.clickable-row")
    expect(rows).to_have_count(50)
    expect(operator_page.locator("#fleet-page-info")).to_contain_text("~130 hubs")
    expect(operator_page.locator("#fleet-prev")).to_have_count(0)
    _shot(operator_page, "fleet_operator_1400.png")

    operator_page.locator("#fleet-next").click()
    expect(rows.first).to_have_attribute("data-row-id", "hub-0051")
    assert "cursor=" in operator_page.url
    operator_page.locator("#fleet-prev").click()
    expect(rows.first).to_have_attribute("data-row-id", "hub-0001")

    operator_page.locator("#fleet-page-size").select_option(label="25")
    expect(rows).to_have_count(25)
    assert "size=25" in operator_page.url


def test_filters_become_chips_live_in_the_url_and_survive_refresh(operator_page: Page) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/fleet")
    operator_page.locator("#flt-zone summary").click()
    operator_page.locator('#flt-zone input[value="LZ_NORTH"]').check()
    operator_page.locator("#flt-health summary").click()
    operator_page.locator('#flt-health input[value="OK"]').check()
    operator_page.get_by_role("button", name="Apply filters").click()

    assert "zone=LZ_NORTH" in operator_page.url and "health=OK" in operator_page.url
    chips = operator_page.locator("#fleet-chips .fl-chip")
    expect(chips).to_have_count(2)
    for zone in operator_page.locator("#fleet-table tbody tr td:nth-child(4)").all_inner_texts():
        assert zone.strip() == "LZ_NORTH"

    operator_page.reload()
    expect(chips).to_have_count(2)

    operator_page.locator("#fleet-chips .fl-chip", has_text="Health: OK").click()
    expect(chips).to_have_count(1)
    assert "health=" not in operator_page.url
    operator_page.locator("#fleet-filter-reset").click()
    expect(chips).to_have_count(0)


def test_sorting_is_a_link_in_the_url(operator_page: Page) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/fleet?sort=hub&dir=desc")
    expect(operator_page.locator("#fleet-table tbody tr.clickable-row").first).to_have_attribute(
        "data-row-id", "hub-0130"
    )
    expect(operator_page.locator('th[aria-sort="descending"]')).to_contain_text("Hub")


def test_typeahead_is_keyboard_navigable_and_fills_the_bank(operator_page: Page) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/fleet")
    hub = operator_page.locator("#cmd-hub")
    hub.fill("hub-000")
    listbox = operator_page.locator("#cmd-hub-list")
    expect(listbox).to_be_visible()
    expect(listbox.locator("[role=option]").first).to_contain_text("LZ_SOUTH")
    hub.press("ArrowDown")
    hub.press("ArrowDown")
    hub.press("Enter")
    expect(hub).to_have_value("hub-0002")
    expect(operator_page.locator("#cmd-bank")).to_have_value("bank-01")
    expect(operator_page.locator("#cmd-setpoint")).to_have_attribute("max", "11")
    expect(operator_page.locator("#cmd-setpoint-hint")).to_contain_text("11 kW")


def test_safe_stop_scope_id_follows_the_scope(operator_page: Page) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/fleet")
    scope_id = operator_page.locator("#stop-scope-id")
    expect(scope_id).to_be_disabled()
    operator_page.locator("#stop-scope").select_option("bank")
    expect(scope_id).to_be_enabled()
    scope_id.fill("bank-0")
    expect(operator_page.locator("#stop-scope-id-list [role=option]").first).to_contain_text("bank-0")
    operator_page.locator("#stop-scope").select_option("zone")
    scope_id.fill("LZ_H")
    expect(operator_page.locator("#stop-scope-id-list [role=option]").first).to_contain_text("LZ_HOUSTON")


def test_release_request_id_is_a_list_of_pending_requests(operator_page: Page) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/fleet")
    select = operator_page.locator("#rel-request-id")
    expect(select.locator("option")).to_have_count(1)
    expect(select.locator("option").first).to_contain_text("ZONE:LZ_SOUTH")
    expect(select.locator("option").first).to_contain_text("alice")
    expect(operator_page.locator("#release-review-button")).to_be_enabled()


def test_select_page_and_all_matching_share_one_selection(operator_page: Page) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/fleet")
    count = operator_page.locator("#fleet-selection-count")
    bar = operator_page.locator("#fleet-selbar")
    expect(bar).to_be_hidden()

    operator_page.locator("#fleet-select-all-page").check()
    expect(count).to_have_text("50 hubs selected")
    expect(bar).to_be_visible()
    expect(operator_page.locator("#bulk-propose-button")).to_be_enabled()

    operator_page.locator("#fleet-select-matching").click()
    expect(count).to_have_text("100 hubs selected")
    expect(operator_page.locator("#fleet-selection-note")).to_contain_text("Capped at 100")

    goto_ok(operator_page, f"{BASE_PATH}/fleet?zone=LZ_WEST")
    operator_page.locator("#fleet-select-matching").click()
    expect(count).to_have_text("32 hubs selected")
    ids = operator_page.locator("#bulk-hub-ids").input_value().split(",")
    assert len(ids) == 32

    operator_page.locator("#fleet-selbar-clear").click()
    expect(count).to_have_text("No hubs selected")
    expect(bar).to_be_hidden()


def test_row_click_opens_the_drawer_and_escape_closes_it(operator_page: Page) -> None:
    operator_page.set_viewport_size({"width": 1400, "height": 1000})
    goto_ok(operator_page, f"{BASE_PATH}/fleet")
    drawer = operator_page.locator("#hub-drawer")
    expect(drawer).to_be_hidden()
    operator_page.locator("#fleet-table tbody tr.clickable-row").first.click()
    expect(drawer).to_be_visible()
    expect(drawer.locator("h2")).to_contain_text("Hub hub-0001")
    for section in ("Status", "Last message", "Alerts", "Location", "Asset", "Control"):
        expect(drawer.locator(f'section[aria-label="{section}"]')).to_have_count(1)
    expect(drawer).to_contain_text("not recorded")
    _shot(operator_page, "fleet_drawer.png")
    operator_page.keyboard.press("Escape")
    expect(drawer).to_be_hidden()


@pytest.mark.parametrize(
    "form", ["#manual-command-form", "#bulk-command-form", "#safestop-propose-form", "#release-request-form"]
)
def test_action_controls_share_one_baseline(operator_page: Page, form: str) -> None:
    operator_page.set_viewport_size({"width": 1400, "height": 1000})
    goto_ok(operator_page, f"{BASE_PATH}/fleet")
    fields = operator_page.locator(f"{form} .fl-field")
    bottoms: dict[float, list[float]] = {}
    for i in range(fields.count()):
        box = fields.nth(i).bounding_box()
        assert box is not None
        row = round(box["y"] / 20)
        bottoms.setdefault(row, []).append(box["y"] + box["height"])
    for row_bottoms in bottoms.values():
        assert max(row_bottoms) - min(row_bottoms) <= 2, row_bottoms
    label_tops = [
        b["y"]
        for b in (
            fields.nth(i).locator("label, .fl-label").first.bounding_box() for i in range(fields.count())
        )
        if b
    ]
    assert label_tops
    suffix = operator_page.locator(f"{form} .og-input-suffix")
    if suffix.count():
        group = operator_page.locator(f"{form} .og-input-group").first.bounding_box()
        sbox = suffix.first.bounding_box()
        assert group and sbox and sbox["x"] + sbox["width"] <= group["x"] + group["width"]
    if form == "#manual-command-form":
        _shot(operator_page, "fleet_actions_1400.png")


def test_disabled_propose_says_why(operator_page: Page) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/fleet")
    form = operator_page.locator("#manual-command-form")
    button = form.get_by_role("button", name="Propose (step 1 of 2)")
    expect(button).to_be_disabled()
    expect(form.locator(".fl-why")).to_contain_text("A reason is required.")
    form.get_by_label("Hub id", exact=True).fill("hub-0001")
    form.get_by_label("Setpoint (kW)", exact=True).fill("3")
    form.get_by_label("Reason", exact=True).fill("test")
    expect(button).to_be_enabled()


def test_viewer_is_read_only(viewer_page: Page) -> None:
    goto_ok(viewer_page, f"{BASE_PATH}/fleet")
    expect(viewer_page.locator("#fleet-table tbody tr.clickable-row")).to_have_count(50)
    for selector in (
        "input.og-row-select",
        "#fleet-select-all-page",
        "#fleet-select-matching",
        "#fleet-actions",
        "#fleet-selbar",
    ):
        expect(viewer_page.locator(selector)).to_have_count(0)
    assert viewer_page.request.get(f"{BASE_PATH}/fleet/selection").status == 403
    _shot(viewer_page, "fleet_viewer.png")


def test_390px_has_no_horizontal_overflow(operator_page: Page) -> None:
    operator_page.set_viewport_size({"width": 390, "height": 900})
    goto_ok(operator_page, f"{BASE_PATH}/fleet")
    overflow = operator_page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 0, overflow
    _shot(operator_page, "fleet_390.png")
