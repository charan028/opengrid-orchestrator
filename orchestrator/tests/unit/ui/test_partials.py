"""Template-rendering tests for the shared `_partials/` (BUILD.md task brief: "template rendering")."""

from __future__ import annotations

from opengrid.ui.templating import templates


def test_kpi_tile_renders_value_unit_and_age() -> None:
    html = templates.env.get_template("_partials/kpi_tile.html").render(
        label="Fleet power", value=4.2, unit="MW", age_s=3.4
    )
    assert "Fleet power" in html
    assert "4.2" in html
    assert "MW" in html
    assert "age: 3s" in html


def test_status_badge_never_uses_colour_alone() -> None:
    html = templates.env.get_template("_partials/status_badge.html").render(status="fault")
    assert "status-fault" in html
    assert "fault" in html
    # an icon glyph must be present alongside the colour class (colour-blind safety, UI-UX spec S5)
    assert "status-icon" in html


def test_data_table_renders_rows_and_empty_state() -> None:
    template = templates.env.get_template("_partials/data_table.html")
    columns = [{"key": "a", "label": "A"}, {"key": "b", "label": "B", "numeric": True}]

    with_rows = template.render(columns=columns, rows=[{"a": "x", "b": 1}])
    assert "<td>x</td>" in with_rows or "x" in with_rows
    assert "1" in with_rows

    empty = template.render(columns=columns, rows=[], empty_message="Nothing here")
    assert "Nothing here" in empty


def test_confirm_dialog_is_two_step() -> None:
    html = templates.env.get_template("_partials/confirm_dialog.html").render(
        dialog_id="d1",
        trigger_label="Safe stop: bank-01",
        title="Confirm",
        summary="This will stop bank-01.",
        confirm_url="/og/api/safestop/bank/bank-01/confirm",
        variant="safestop",
    )
    assert "Safe stop: bank-01" in html
    assert 'hx-post="/og/api/safestop/bank/bank-01/confirm"' in html
    assert "btn-safestop" in html


def test_stale_badge_flags_stale_values() -> None:
    template = templates.env.get_template("_partials/stale_badge.html")

    fresh = template.render(age_s=5, stale_after_s=600)
    stale = template.render(age_s=900, stale_after_s=600)
    unknown = template.render(age_s=None)

    assert 'data-stale="false"' in fresh
    assert 'data-stale="true"' in stale
    assert "age: unknown" in unknown
