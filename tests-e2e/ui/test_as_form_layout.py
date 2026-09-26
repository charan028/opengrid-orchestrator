"""Dispatch's AS deployment form (owner review, R2): one baseline for every control, no fleet-wide option,
duration capped by the chosen award's product, and a row's Deploy button preselecting that award."""

from __future__ import annotations

import os
from itertools import pairwise

from playwright.sync_api import Page, expect

from screens import BASE_PATH, goto_ok

_CONTROLS = ("#as-award-select", ".og-input-group", ".as-field-reason input", ".as-submit")


def _bottoms(page: Page) -> list[float]:
    form = page.locator("#as-deployment-form")
    boxes = [form.locator(sel).first.bounding_box() for sel in _CONTROLS]
    assert all(boxes), boxes
    return [b["y"] + b["height"] for b in boxes if b]


def test_controls_share_one_baseline_and_units_are_inline(operator_page: Page) -> None:
    operator_page.set_viewport_size({"width": 1280, "height": 900})
    goto_ok(operator_page, f"{BASE_PATH}/dispatch")
    form = operator_page.locator("#as-deployment-form")
    expect(form).to_be_visible()
    bottoms = _bottoms(operator_page)
    assert max(bottoms) - min(bottoms) <= 2, bottoms
    group = form.locator(".og-input-group").bounding_box()
    suffix = form.locator(".og-input-suffix").bounding_box()
    assert group and suffix and abs((suffix["y"] + suffix["height"]) - (group["y"] + group["height"])) <= 1
    shots = os.environ.get("OG_UI_SCREENSHOT_DIR")
    if shots:
        form.screenshot(path=os.path.join(shots, "as_deployment_form.png"))
        operator_page.locator('section[aria-labelledby="as-heading"]').screenshot(
            path=os.path.join(shots, "as_deployment_panel.png")
        )


def test_no_fleet_wide_option_and_the_cap_follows_the_product(operator_page: Page) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/dispatch")
    select = operator_page.locator("#as-award-select")
    expect(select.locator("option")).not_to_contain_text(["all held"])
    expect(select.locator('option[value=""]')).to_have_count(0)
    duration = operator_page.locator("#as-duration")
    ecrs = select.locator('option[data-product="ECRS"]').first.get_attribute("value")
    nspin = select.locator('option[data-product="NSPIN"]').first.get_attribute("value")
    select.select_option(ecrs)
    expect(duration).to_have_attribute("max", "60")
    expect(operator_page.locator("#as-duration-hint")).to_have_text("Up to 60 min for ECRS.")
    select.select_option(nspin)
    expect(duration).to_have_attribute("max", "240")


def test_row_deploy_preselects_its_award(operator_page: Page) -> None:
    goto_ok(operator_page, f"{BASE_PATH}/dispatch")
    rows = operator_page.locator(".as-row-deploy")
    target = rows.nth(rows.count() - 1)
    oid = target.get_attribute("data-obligation")
    target.click()
    expect(operator_page.locator("#as-award-select")).to_have_value(oid or "")
    expect(operator_page.locator("#as-duration")).to_be_focused()


def test_narrow_width_stacks_without_overlap(operator_page: Page) -> None:
    operator_page.set_viewport_size({"width": 390, "height": 900})
    goto_ok(operator_page, f"{BASE_PATH}/dispatch")
    form = operator_page.locator("#as-deployment-form")
    boxes = [form.locator(sel).first.bounding_box() for sel in _CONTROLS]
    assert all(boxes)
    tops = sorted((b["y"], b["y"] + b["height"]) for b in boxes if b)
    for (_, bottom), (next_top, _) in pairwise(tops):
        assert next_top >= bottom - 1, tops
    shots = os.environ.get("OG_UI_SCREENSHOT_DIR")
    if shots:
        form.screenshot(path=os.path.join(shots, "as_deployment_form_narrow.png"))
