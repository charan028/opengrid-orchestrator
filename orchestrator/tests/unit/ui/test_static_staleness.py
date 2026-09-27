"""Static check (BUILD.md task brief: "a static check that every page includes the staleness
indicators"): every top-level screen template ui-a owns must include the `stale_badge` partial (directly
or via `kpi_tile`, which itself includes it), so every value on screen carries a visible data-age
indicator."""

from __future__ import annotations

from pathlib import Path

import pytest

TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "src" / "opengrid" / "ui" / "templates"

# ui-a's own screens (BUILD.md S4). ui-b's screens are checked once they land, not required here.
UI_A_SCREENS = ["story.html", "control_room.html", "fleet.html", "health.html"]


@pytest.mark.parametrize("screen", UI_A_SCREENS)
def test_screen_includes_a_staleness_indicator(screen: str) -> None:
    text = (TEMPLATES_DIR / screen).read_text(encoding="utf-8")
    assert "stale_badge" in text or "kpi_tile" in text, (
        f"{screen} must include stale_badge.html (directly or via kpi_tile.html) so every value shows "
        "its age (BUILD.md UI brief)"
    )


def test_fleet_hub_drilldown_partial_shows_age() -> None:
    text = (TEMPLATES_DIR / "_partials" / "hub_drilldown.html").read_text(encoding="utf-8")
    assert "data-since" in text


def test_base_template_defines_the_required_blocks() -> None:
    text = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
    for block in ["title", "header_actions", "content", "scripts"]:
        assert f"block {block}" in text
