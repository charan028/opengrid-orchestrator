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
    expect(rows).to_have_count(25)
    expect(operator_page.locator("#fleet-page-info")).to_contain_text("~132 hubs")
    expect(operator_page.locator("#fleet-prev")).to_have_count(0)
    _shot(operator_page, "fleet_operator_1400.png")

    operator_page.locator("#fleet-next").click()
    expect(rows.first).to_have_attribute("data-row-id", "hub-0026")
    assert "cursor=" in operator_page.url
    operator_page.locator("#fleet-prev").click()
    expect(rows.first).to_have_attribute("data-row-id", "hub-0001")

    operator_page.locator("#fleet-page-size").select_option(label="50")
    expect(rows).to_have_count(50)
    assert "size=50" in operator_page.url
    expect(operator_page.locator("#fleet-page-size option")).to_have_count(2)


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
        "data-row-id", "trailer-mb-01"
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
    expect(count).to_have_text("25 hubs selected")
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
    expect(viewer_page.locator("#fleet-table tbody tr.clickable-row")).to_have_count(25)
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


# -- R3.1 -----------------------------------------------------------------------------------------


def test_hw_fw_columns_filters_and_out_of_date_chip(operator_page: Page) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/fleet?fw_not=4.3.0&hw=C1")
    chips = operator_page.locator("#fleet-chips .fl-chip")
    expect(chips).to_have_count(2)
    expect(chips.filter(has_text="FW \u2260 4.3.0")).to_have_count(1)
    header = operator_page.locator("#fleet-table thead")
    expect(header).to_contain_text("HW rev")
    expect(header).to_contain_text("FW version")
    for fw in operator_page.locator("#fleet-table tbody tr td:nth-child(11)").all_inner_texts():
        assert fw.strip() == "4.2.1"
    for hw in operator_page.locator("#fleet-table tbody tr td:nth-child(10)").all_inner_texts():
        assert hw.strip() == "C1"
    operator_page.locator("#flt-fw summary").click()
    expect(operator_page.locator('#flt-fw input[value="4.3.0"]')).to_have_count(1)
    goto_ok(operator_page, f"{BASE_PATH}/fleet?sort=fw&dir=desc")
    expect(operator_page.locator('th[aria-sort="descending"]')).to_contain_text("FW version")


def test_operator_targets_are_marked_in_the_table_and_drawer(operator_page: Page) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/fleet")
    row = operator_page.locator('tr[data-row-id="hub-0003"]')
    expect(row.locator(".fl-target")).to_contain_text("target 5.0 kW")
    expect(operator_page.locator("#fleet-page-info")).to_contain_text("1 under an operator target")
    expect(operator_page.locator('tr[data-row-id="hub-0004"] .fl-target')).to_have_count(0)
    row.click()
    drawer = operator_page.locator("#hub-drawer")
    expect(drawer.locator("#drawer-target")).to_contain_text("5.0 kW")
    expect(drawer.locator("#drawer-hwfw")).to_contain_text("HW rev")
    expect(drawer.locator("#drawer-charge-window")).to_contain_text("from BANK:bank-03")
    expect(drawer).to_contain_text("Reported by battery")


def test_manual_command_ramps_and_can_be_cancelled(operator_page: Page) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/fleet")
    form = operator_page.locator("#manual-command-form")
    expect(form.get_by_label("Expires in", exact=True)).to_have_value("15")
    form.get_by_label("Hub id", exact=True).fill("hub-0001")
    form.get_by_label("Setpoint (kW)", exact=True).fill("5")
    form.get_by_label("Reason", exact=True).fill("ramp test")
    form.get_by_role("button", name="Propose (step 1 of 2)").click()
    operator_page.locator("#command-propose-result .confirm-dialog").get_by_role(
        "button", name="Send command"
    ).click()
    ramp = operator_page.locator("#command-confirm-result .fl-ramp")
    expect(ramp).to_contain_text("Ramping 1 hub to")
    expect(ramp).to_contain_text("5.0 kW")
    expect(ramp.locator(".fl-ramp-now")).to_contain_text("Now ", timeout=8000)
    _shot(operator_page, "fleet_ramp.png")
    ramp.get_by_role("button", name="Cancel target").click()
    expect(operator_page.locator("#command-confirm-result .status-text")).to_have_text("CANCELLED")


def test_charging_schedule_lists_previews_and_saves_in_two_steps(operator_page: Page) -> None:
    operator_page.set_viewport_size({"width": 1400, "height": 1000})
    goto_ok(operator_page, f"{BASE_PATH}/fleet")
    card = operator_page.locator("#charge-schedule")
    expect(card).to_contain_text("Solar charging is always allowed")
    expect(card.locator('[data-scope="FLEET:*"]')).to_contain_text("22:00-06:00")
    expect(card.locator('[data-scope="BANK:bank-03"]')).to_contain_text("changed by alice")

    card.locator("#cw-preview-hub").fill("hub-0003")
    card.get_by_role("button", name="Preview").click()
    expect(card.locator("#charge-effective")).to_contain_text("from BANK:bank-03")

    ref = card.locator("#cw-scope-ref")
    expect(ref).to_be_disabled()
    card.locator("#cw-scope").select_option("BANK")
    expect(ref).to_be_enabled()
    ref.fill("bank-05")
    card.locator("#cw-add").click()
    windows = card.locator(".fl-cw-window")
    expect(windows).to_have_count(2)
    windows.nth(1).locator(".fl-cw-del").click()
    windows.nth(0).locator("input").nth(0).fill("21:00")
    windows.nth(0).locator("input").nth(1).fill("05:00")
    submit = card.get_by_role("button", name="Review change (step 1 of 2)")
    expect(submit).to_be_disabled()
    card.locator("#cw-reason").fill("tariff change")
    expect(submit).to_be_enabled()
    submit.click()
    dialog = card.locator("#charge-propose-result .confirm-dialog")
    expect(dialog).to_contain_text("21:00-05:00")
    _shot(operator_page, "fleet_charging.png")
    dialog.get_by_role("button", name="Save schedule").click()
    expect(card.locator("#charge-confirm-result .status-text")).to_have_text("SAVED")


def test_viewer_reads_the_charging_schedule_but_cannot_edit(viewer_page: Page) -> None:
    goto_ok(viewer_page, f"{BASE_PATH}/fleet")
    expect(viewer_page.locator("#charge-list")).to_contain_text("22:00-06:00")
    expect(viewer_page.locator("#charge-edit-form")).to_have_count(0)
    expect(viewer_page.locator("#charge-schedule button", has_text="Remove")).to_have_count(0)


def test_asset_types_on_the_map_table_filter_and_drawer(operator_page: Page) -> None:
    operator_page.set_viewport_size({"width": 1400, "height": 1000})
    goto_ok(operator_page, f"{BASE_PATH}/fleet?asset_class=MOBILE&asset_class=UTILITY_SCALE")
    rows = operator_page.locator("#fleet-table tbody tr.clickable-row")
    expect(rows).to_have_count(2)
    expect(operator_page.locator("#fleet-chips .fl-chip")).to_have_count(2)
    expect(operator_page.locator("#fleet-table thead")).to_contain_text("Asset type")
    expect(operator_page.locator('tr[data-row-id="trailer-mb-01"] .fl-asset-cell')).to_contain_text("Truck")
    expect(operator_page.locator('tr[data-row-id="sub-LZ_AEN-00"] .fl-asset-cell')).to_contain_text(
        "Substation BESS"
    )

    fleet_map = operator_page.locator("#fleet-map")
    expect(fleet_map.locator(".og-asset-truck")).to_have_count(1)
    expect(fleet_map.locator(".og-asset-sub")).to_have_count(1)
    expect(fleet_map.locator(".og-asset-depot")).to_have_count(1)
    legend = operator_page.locator(".og-map-legend")
    for label in ("Home batteries", "Trucks", "Substation BESS", "Home stations"):
        expect(legend).to_contain_text(label)
    toggle = operator_page.get_by_label("Show Trucks (mobile storage) layer")
    toggle.uncheck()
    expect(fleet_map.locator(".og-asset-truck")).to_have_count(0)
    toggle.check()
    fleet_map.scroll_into_view_if_needed()
    _shot(operator_page, "fleet_asset_types.png")

    rows.filter(has_text="trailer-mb-01").click()
    drawer = operator_page.locator("#hub-drawer")
    expect(drawer.locator("#drawer-asset-type")).to_contain_text("Truck")
    truck = drawer.locator("#drawer-truck")
    expect(truck).to_contain_text("hs-austin-north-01 (LZ_AEN)")
    expect(truck).to_contain_text("Away from home station")
    expect(truck).to_contain_text("only at its home station")
    _shot(operator_page, "fleet_drawer_truck.png")
    operator_page.keyboard.press("Escape")
    rows.filter(has_text="sub-LZ_AEN-00").click()
    expect(drawer.locator("#drawer-substation")).to_contain_text("4.0 MW / 16.0 MWh")
    expect(drawer.locator("#drawer-substation")).to_contain_text("F-AEN-7")


def test_control_room_map_shapes_asset_classes(operator_page: Page) -> None:
    operator_page.set_viewport_size({"width": 1400, "height": 1000})
    goto_ok(operator_page, f"{BASE_PATH}/")
    grid_map = operator_page.locator("#fleet-map")
    expect(grid_map.locator(".og-asset-truck")).to_have_count(1)
    expect(grid_map.locator(".og-asset-sub")).to_have_count(1)
    expect(grid_map.locator(".og-asset-depot")).to_have_count(1)
    legend = operator_page.locator(".og-map-legend")
    for label in ("Trucks", "Substation BESS", "Home stations"):
        expect(legend).to_contain_text(label)
    toggle = operator_page.get_by_label("Show substation BESS layer")
    toggle.uncheck()
    expect(grid_map.locator(".og-asset-sub")).to_have_count(0)
    toggle.check()
    grid_map.scroll_into_view_if_needed()
    shots = os.environ.get("OG_UI_SCREENSHOT_DIR")
    if shots:
        grid_map.screenshot(path=os.path.join(shots, "control_room_assets.png"))


def test_layout_order_map_hubs_charging_then_the_rest(operator_page: Page) -> None:
    """Owner r3.4: map first, then the hubs table, then the charging schedule, then everything else."""
    for width in (1400, 390):
        operator_page.set_viewport_size({"width": width, "height": 900})
        goto_ok(operator_page, f"{BASE_PATH}/fleet")
        tops = [
            operator_page.locator(sel).first.bounding_box()
            for sel in (
                ".og-map-panel",
                'section[aria-labelledby="hubs-heading"]',
                "#charge-schedule",
                ".fl-summary",
                "#fleet-actions",
            )
        ]
        assert all(tops), tops
        ys = [b["y"] for b in tops if b]
        assert ys == sorted(ys), ys
        lefts = {round(b["x"]) for b in tops if b}
        assert len(lefts) == 1, lefts  # one left edge: the panels line up
        overflow = operator_page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 0, (width, overflow)
        _shot(operator_page, f"fleet_layout_{width}.png")


def test_power_column_refreshes_live(operator_page: Page) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/fleet")
    cell = operator_page.locator('tr[data-row-id="hub-0001"] td[data-col=kw]')
    original = cell.inner_text()
    operator_page.evaluate(
        "document.querySelector('tr[data-row-id=\\'hub-0001\\'] td[data-col=kw]').textContent = 'x'"
    )
    expect(cell).to_have_text("x")
    operator_page.evaluate("ogFleet.refreshPower()")
    expect(cell).to_have_text(original)


def test_select_matching_is_clamped_to_the_bulk_cap(operator_page: Page) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/fleet")
    button = operator_page.locator("#fleet-select-matching")
    expect(button).to_contain_text("up to 500")
    button.click()
    expect(operator_page.locator("#fleet-selection-note")).to_contain_text("the most one bulk command takes")


def test_tolling_obligation_gets_a_utility_call(operator_page: Page) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/dispatch")
    call = operator_page.locator('.as-row-deploy[data-obligation="7011aaaa-0000-0000-0000-0000000070aa"]')
    expect(call).to_have_text("Utility call\u2026")
    call.click()
    expect(operator_page.locator("#as-duration")).to_have_attribute("max", "90")
    expect(operator_page.locator("#as-duration-hint")).to_have_text("Up to 90 min for TOLLING.")
    operator_page.locator("#as-deployment-form").get_by_role("button", name="Propose deployment").click()
    expect(operator_page.locator("#as-action-result")).to_contain_text(
        "utility's call on tolling obligation 7011aaaa"
    )
