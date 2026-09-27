"""D-37 (owner requirement): the reason LZ_LCRA/LZ_RAYBN batteries can't be used is SHOWN, in the owner's words
-- badge "Regulated market - no contract" plus the tooltip -- and their kW is a separate line, never in the
available kW. Profitability's capacity line and the Control room zone summary. (The Fleet table/map/drawer badge
for an LCRA hub is UI-FLEET's integ/fleet-noie; its Playwright test lives there.)"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from screens import BASE_PATH, goto_ok

BADGE = "Regulated market – no contract"  # noqa: RUF001 -- the owner's wording
TOOLTIP = (
    "Unavailable: regulated (NOIE) territory, so energy can't be sold into ERCOT, and there is no utility "
    "capacity contract to reserve it. Available once a contract is signed."
)


def test_profitability_shows_the_unavailable_capacity_line_with_the_reason(viewer_page: Page) -> None:
    goto_ok(viewer_page, f"{BASE_PATH}/profitability")
    line = viewer_page.locator("#profitability-capacity-unavailable")
    expect(line).to_be_visible()
    expect(line).to_contain_text("Available: 30000 kW")
    expect(line).to_contain_text(f"{BADGE}: 12000 kW")
    badge = line.locator(".og-unavail")
    expect(badge).to_have_text(BADGE)
    expect(badge).to_have_attribute("title", TOOLTIP)


def test_control_room_zone_summary_names_lcra_and_raybn_with_the_badge(viewer_page: Page) -> None:
    goto_ok(viewer_page, f"{BASE_PATH}/")
    zones = viewer_page.locator("#control-room-zone-unavailable-zones li")
    expect(zones).to_have_count(2)
    expect(zones.nth(0)).to_contain_text("LZ_LCRA: 10/10 banks, 6000 kW")
    expect(zones.nth(0)).to_contain_text(BADGE)
    expect(zones.nth(0)).to_have_attribute("title", TOOLTIP)
    expect(zones.nth(1)).to_contain_text("LZ_RAYBN")
