"""The Fleet map's selection and the bulk manual command (CR #19 item 2).

Covers the path an operator actually takes: pick hubs in the table, watch the map and the counter agree,
propose for the selection, and meet the second acknowledgement that a high-risk selection demands before
the confirm button will act.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from screens import BASE_PATH, goto_ok


def _select_first_rows(page: Page, count: int) -> None:
    boxes = page.locator("input.og-row-select")
    for index in range(count):
        boxes.nth(index).check()


def test_fleet_map_and_selection_counter_track_the_table(operator_page: Page) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/fleet")

    expect(
        operator_page.locator("#fleet-map .leaflet-container, #fleet-map.leaflet-container")
    ).to_have_count(1)
    expect(operator_page.locator("#fleet-selection-count")).to_have_text("No hubs selected")
    expect(operator_page.locator("#bulk-propose-button")).to_be_disabled()

    _select_first_rows(operator_page, 2)

    expect(operator_page.locator("#fleet-selection-count")).to_have_text("2 hubs selected")
    expect(operator_page.locator("#bulk-propose-button")).to_be_enabled()
    expect(operator_page.locator("#bulk-hub-ids")).to_have_value("hub-0001,hub-0002")

    operator_page.locator("#fleet-select-clear").click()
    expect(operator_page.locator("#fleet-selection-count")).to_have_text("No hubs selected")
    expect(operator_page.locator("#bulk-propose-button")).to_be_disabled()


def test_bulk_command_needs_a_second_acknowledgement_then_passes(operator_page: Page) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/fleet")
    _select_first_rows(operator_page, 2)

    form = operator_page.locator("#bulk-command-form")
    form.get_by_label("Setpoint (kW)", exact=True).fill("5.0")
    form.get_by_label("Reason", exact=True).fill("load test")
    form.get_by_role("button", name="Propose for selection (step 1 of 2)").click()

    dialog = operator_page.locator("#bulk-propose-result .confirm-dialog")
    expect(dialog).to_be_visible()
    confirm = dialog.get_by_role("button", name="Send to selection")

    # the API flagged the selection, so the confirm button does nothing until the reason is acknowledged
    acknowledge = dialog.locator(".confirm-dialog-ack input[type=checkbox]")
    expect(acknowledge).to_be_visible()
    expect(confirm).to_be_disabled()

    # the acknowledgement names the API's own reasons, not only the console's reading
    expect(dialog.locator(".confirm-dialog-ack")).to_contain_text(
        "serving a committed obligation (ERCOT_ENERGY)"
    )
    acknowledge.check()
    expect(confirm).to_be_enabled()
    confirm.click()

    # the API recorded the first confirm and asks for a second, separate one; nothing is sent automatically
    result = operator_page.locator("#bulk-confirm-result")
    expect(result.locator(".status-badge .status-text")).to_have_text("SECOND CONFIRM")
    expect(result).to_contain_text("1 hub in a fault state")
    result.locator("#bulk-second-confirm").click()
    expect(operator_page.locator("#bulk-confirm-result .status-badge .status-text")).to_have_text("PASS")
    expect(operator_page.locator("#bulk-confirm-result")).to_contain_text("3 of 3 hubs passed the guardian")


def test_viewer_gets_no_selection_or_bulk_command(viewer_page: Page) -> None:
    goto_ok(viewer_page, f"{BASE_PATH}/fleet")

    expect(viewer_page.locator("input.og-row-select")).to_have_count(0)
    expect(viewer_page.locator("#bulk-command-form")).to_have_count(0)
    expect(viewer_page.locator("#fleet-select-toggle")).to_have_count(0)


def _hub_screen_point(page: Page, index: int = 0) -> tuple[str, float, float]:
    """The id and viewport position of a real hub marker, read off the live map (`og.map.instances`).
    Clicking a guessed fraction of the map hits empty space more often than not, which is exactly how a
    working click-to-select looked broken during review."""
    page.locator("#fleet-map").scroll_into_view_if_needed()
    box = page.locator("#fleet-map").bounding_box()
    assert box is not None
    probe = page.evaluate(
        """(index) => {
            const handle = og.map.instances['fleet-map'];
            const ids = Object.keys(handle.hubMarkers).sort();
            const id = ids[index];
            const point = handle.map.latLngToContainerPoint(handle.hubMarkers[id].getLatLng());
            return {id: id, x: point.x, y: point.y};
        }""",
        index,
    )
    return probe["id"], box["x"] + probe["x"], box["y"] + probe["y"]


def test_clicking_a_hub_opens_its_detail_when_not_selecting(operator_page: Page) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/fleet")
    _hub_id, x, y = _hub_screen_point(operator_page)

    operator_page.mouse.click(x, y)

    expect(operator_page.locator(".leaflet-popup")).to_have_count(1)
    expect(operator_page.locator("#fleet-selection-count")).to_have_text("No hubs selected")


def test_clicking_a_hub_in_select_mode_picks_and_unpicks_it(operator_page: Page) -> None:
    """CR #19 asks for "one or in bulk": one click is the "one"."""
    goto_ok(operator_page, f"{BASE_PATH}/fleet")
    operator_page.locator("#fleet-select-toggle").click()
    hub_id, x, y = _hub_screen_point(operator_page)

    operator_page.mouse.click(x, y)
    expect(operator_page.locator("#fleet-selection-count")).to_have_text("1 hub selected")
    expect(operator_page.locator("#bulk-hub-ids")).to_have_value(hub_id)

    operator_page.mouse.click(x, y)
    expect(operator_page.locator("#fleet-selection-count")).to_have_text("No hubs selected")


def test_a_click_on_empty_map_keeps_the_selection(operator_page: Page) -> None:
    """A stray click used to sweep a zero-area rectangle and silently clear everything selected."""
    goto_ok(operator_page, f"{BASE_PATH}/fleet")
    _select_first_rows(operator_page, 2)
    operator_page.locator("#fleet-select-toggle").click()
    operator_page.locator("#fleet-map").scroll_into_view_if_needed()
    box = operator_page.locator("#fleet-map").bounding_box()
    assert box is not None

    # the far top-left of the ERCOT view is open water/desert -- no hub within the pick radius
    operator_page.mouse.click(box["x"] + 12, box["y"] + 12)

    expect(operator_page.locator("#fleet-selection-count")).to_have_text("2 hubs selected")


def test_the_map_legend_names_every_layer(viewer_page: Page) -> None:
    goto_ok(viewer_page, f"{BASE_PATH}/fleet")
    legend = viewer_page.locator(".og-map-legend")

    expect(legend).to_contain_text("Hub health")
    for state in ("Online", "Stale", "Offline", "Fault"):
        expect(legend).to_contain_text(state)
    expect(legend).to_contain_text("selected for a command")
