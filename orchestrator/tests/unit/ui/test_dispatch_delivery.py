"""D-38 on the Dispatch page: a Delivery cell per deployed call (filled from /og/api/delivery/records by
static/og-delivery.js) and the detail drawer partial."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

UI = Path(__file__).resolve().parents[3] / "src" / "opengrid" / "ui"


def test_the_awards_table_has_a_delivery_cell_only_for_deployed_calls() -> None:
    source = (UI / "templates" / "dispatch.html").read_text(encoding="utf-8")

    assert "<th>Risk</th><th>Delivery</th><th>Action</th>" in source
    assert '{% if award.deployment_id %} data-delivery-call="{{ award.deployment_id }}"{% endif %}' in source
    assert '{% include "_partials/delivery_drawer.html" %}' in source


def test_the_drawer_partial_loads_the_delivery_script_under_the_base_path() -> None:
    env = Environment(loader=FileSystemLoader(str(UI / "templates")), autoescape=True)
    html = env.get_template("_partials/delivery_drawer.html").render(base_path="/og")

    assert 'id="delivery-drawer"' in html and 'id="delivery-drawer-chart"' in html
    assert 'src="/og/static/og-delivery.js" data-base-path="/og"' in html


def test_the_script_reads_the_operator_delivery_api() -> None:
    script = (UI / "static" / "og-delivery.js").read_text(encoding="utf-8")

    assert '"/api/delivery/records?call_ids="' in script and '"/api/delivery/records/"' in script
    assert "data-delivery-call" in script or "deliveryCall" in script
