"""The Help page (`/og/help`, r3.4): the Help entry is the last item of the left menu on every screen and
deep-links to that screen's section; the table of contents, anchors and search filter work; a viewer can
read it; it is readable in both themes and at 390 px; and it raises no console errors."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import ConsoleMessage, Page, expect

from contrast import AA_BODY_TEXT, contrast_ratio
from screens import BASE_PATH, SCREENS, goto_ok

HELP = f"{BASE_PATH}/help"
_SECTION = {
    f"{BASE_PATH}/": "page-control-room",
    f"{BASE_PATH}/fleet": "page-fleet",
    f"{BASE_PATH}/dispatch": "page-dispatch",
    f"{BASE_PATH}/markets": "page-markets",
    f"{BASE_PATH}/health": "page-health",
    f"{BASE_PATH}/profitability": "page-profitability",
    f"{BASE_PATH}/billing": "page-billing",
    f"{BASE_PATH}/pq": "page-pq",
    HELP: "page-help",
}
_ALL_UI_ROUTES = (*[path for path, _h1 in SCREENS], HELP)


def _hex(rgb: str) -> str:
    r, g, b = (int(v) for v in re.findall(r"\d+", rgb)[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


@pytest.mark.parametrize("path", _ALL_UI_ROUTES)
def test_every_route_ends_the_left_menu_with_a_deep_linked_help_entry(viewer_page: Page, path: str) -> None:
    goto_ok(viewer_page, path)
    help_link = viewer_page.locator("nav.og-nav #nav-help")
    expect(help_link).to_have_count(1)
    expect(help_link).to_have_attribute("aria-label", "Help")
    expect(help_link).to_have_attribute("href", f"{HELP}#{_SECTION[path]}")
    # bottom of the menu: below the screens and tools, above the role footer
    link_box = help_link.bounding_box()
    tools_box = viewer_page.locator("#nav-scenarios").bounding_box()
    foot_box = viewer_page.locator(".og-nav-foot").bounding_box()
    assert link_box and tools_box and foot_box
    assert tools_box["y"] < link_box["y"] < foot_box["y"]


@pytest.mark.parametrize("path", [p for p, _h1 in SCREENS])
def test_help_entry_opens_the_help_page_at_the_screen_section(viewer_page: Page, path: str) -> None:
    goto_ok(viewer_page, path)
    viewer_page.locator("#nav-help").focus()
    viewer_page.keyboard.press("Enter")  # keyboard accessible
    viewer_page.wait_for_url(f"**{HELP}#{_SECTION[path]}")
    section = viewer_page.locator(f"#{_SECTION[path]}")
    expect(section).to_be_visible()
    expect(section).to_be_in_viewport()


def test_viewer_reads_the_help_page_without_console_errors(viewer_page: Page) -> None:
    errors: list[str] = []
    viewer_page.on("console", lambda msg: _collect(errors, msg))
    viewer_page.on("pageerror", lambda exc: errors.append(str(exc)))
    goto_ok(viewer_page, HELP)
    expect(viewer_page.locator("h1")).to_have_text("Help")
    expect(viewer_page.locator(".og-nav-role")).to_contain_text("Role: viewer")
    for sec in (
        "overview",
        "architecture",
        "data-model",
        "how-it-works",
        "pages",
        "apis",
        "events",
        "playbooks",
    ):
        expect(viewer_page.locator(f"#{sec}")).to_have_count(1)
    assert errors == []


def _collect(errors: list[str], msg: ConsoleMessage) -> None:
    if msg.type == "error":
        errors.append(msg.text)


def test_every_toc_link_targets_an_existing_anchor(viewer_page: Page) -> None:
    goto_ok(viewer_page, HELP)
    hrefs = viewer_page.locator(".hp-toc a").evaluate_all("els => els.map(e => e.getAttribute('href'))")
    assert len(hrefs) > 30
    missing = [h for h in hrefs if viewer_page.locator(h).count() != 1]
    assert missing == []
    viewer_page.locator(".hp-toc a[href='#ev-guardian']").click()
    viewer_page.wait_for_url("**#ev-guardian")
    expect(viewer_page.locator("#ev-guardian")).to_be_in_viewport()


def test_search_filters_topics_and_rows_and_escape_clears(viewer_page: Page) -> None:
    goto_ok(viewer_page, HELP)
    total = viewer_page.locator(".hp-sub:visible").count()
    search = viewer_page.get_by_label("Search this page")
    search.fill("safe stop")
    expect(viewer_page.locator("#hp-search-status")).to_contain_text("match")
    filtered = viewer_page.locator(".hp-sub:visible").count()
    assert 0 < filtered < total
    expect(viewer_page.locator("#pb-safe-stop")).to_be_visible()
    search.fill("zzqqxx-no-such-topic")
    expect(viewer_page.locator("#hp-no-match")).to_be_visible()
    search.press("Escape")
    expect(viewer_page.locator("#hp-no-match")).to_be_hidden()
    assert viewer_page.locator(".hp-sub:visible").count() == total


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_help_page_is_readable_in_both_themes(viewer_page: Page, theme: str) -> None:
    goto_ok(viewer_page, HELP)
    viewer_page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", theme)
    for selector in (".hp-sec > h2", ".hp-sec p", ".hp-table th", ".hp-table td", ".hp-toc a", ".hp-body a"):
        el = viewer_page.locator(selector).first
        colors = el.evaluate(
            """el => {
              const fg = getComputedStyle(el).color;
              let n = el, bg = 'rgba(0, 0, 0, 0)';
              while (n && (bg === 'rgba(0, 0, 0, 0)' || bg === 'transparent')) {
                bg = getComputedStyle(n).backgroundColor; n = n.parentElement;
              }
              return [fg, bg];
            }"""
        )
        ratio = contrast_ratio(_hex(colors[0]), _hex(colors[1]))
        assert ratio >= AA_BODY_TEXT, f"{theme} {selector}: {ratio:.2f}"
    viewer_page.screenshot(path=_shot(f"help-{theme}.png"))


def test_help_page_fits_390px(viewer_page: Page) -> None:
    viewer_page.set_viewport_size({"width": 390, "height": 844})
    goto_ok(viewer_page, f"{HELP}#page-fleet")
    overflow = viewer_page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 1
    expect(viewer_page.locator("#nav-help")).to_be_visible()
    expect(viewer_page.get_by_label("Search this page")).to_be_visible()
    viewer_page.screenshot(path=_shot("help-390.png"))


def _shot(name: str) -> str:
    import os

    directory = os.environ.get("OG_UI_SCREENSHOT_DIR", "")
    return os.path.join(directory, name) if directory else os.devnull
