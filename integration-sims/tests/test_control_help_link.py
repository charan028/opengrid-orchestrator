"""The sims control page has no console left menu, so its header carries a Help link to the console's
Help page, deep-linked to the Scenarios section (r3.4)."""

from __future__ import annotations

from pathlib import Path

import httpx

from ogsim.control.app import create_app

SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios"
RANDOM_CONFIG = Path(__file__).resolve().parents[1] / "config" / "random.yaml"


async def test_control_page_header_links_to_console_help(tmp_path: Path) -> None:
    app = create_app(
        scenarios_dir=SCENARIOS_DIR,
        random_config_path=RANDOM_CONFIG,
        random_pause_state_path=tmp_path / "random_pause_state.json",
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://control.test") as c:
        resp = await c.get("/")
    assert resp.status_code == 200
    assert 'id="ogsim-help" href="/og/help#page-scenarios" aria-label="Help"' in resp.text
