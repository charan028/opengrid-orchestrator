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

    acknowledge.check()
    expect(confirm).to_be_enabled()
    confirm.click()

    result = operator_page.locator("#bulk-confirm-result")
    expect(result.locator(".status-badge .status-text")).to_have_text("PASS")


def test_viewer_gets_no_selection_or_bulk_command(viewer_page: Page) -> None:
    goto_ok(viewer_page, f"{BASE_PATH}/fleet")

    expect(viewer_page.locator("input.og-row-select")).to_have_count(0)
    expect(viewer_page.locator("#bulk-command-form")).to_have_count(0)
    expect(viewer_page.locator("#fleet-select-toggle")).to_have_count(0)
