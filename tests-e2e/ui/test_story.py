"""The Story screen in a real browser: it is the first screen in the menu, it reads as an argument with
live evidence, every value shows its age, and it is identical for a viewer and an operator (there is
nothing on it to command)."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from screens import BASE_PATH, goto_ok

STORY = f"{BASE_PATH}/story"


def test_story_is_the_first_screen_in_the_menu(viewer_page: Page) -> None:
    goto_ok(viewer_page, f"{BASE_PATH}/")

    first = viewer_page.locator(".og-nav ul li a").first
    expect(first).to_have_attribute("href", STORY)


def test_story_reads_as_thesis_then_evidence(viewer_page: Page) -> None:
    goto_ok(viewer_page, STORY)

    expect(viewer_page.locator("h1")).to_have_text("One fleet, many buyers, homes first")
    headings = viewer_page.locator(".st-section h2")
    expect(headings).to_have_count(6)
    expect(headings.nth(1)).to_contain_text("promise")
    expect(viewer_page.locator(".st-pipeline .st-stage")).to_have_count(5)
    # the price chart initialised on the same helper every other screen uses
    expect(viewer_page.locator("#story-price-chart canvas")).to_have_count(1)


def test_story_has_no_controls_and_is_the_same_for_both_roles(viewer_page: Page, operator_page: Page) -> None:
    goto_ok(viewer_page, STORY)
    goto_ok(operator_page, STORY)

    for page in (viewer_page, operator_page):
        expect(page.locator("main form")).to_have_count(0)
        expect(page.locator("main button")).to_have_count(0)
    assert (
        viewer_page.locator(".st-pipeline").inner_text() == operator_page.locator(".st-pipeline").inner_text()
    )
