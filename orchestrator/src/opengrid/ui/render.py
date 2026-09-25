"""Small helpers to pre-render a base-contract partial to an HTML string for embedding inside a
`data_table.html` cell (columns marked `html: true`) -- e.g. a status badge per table row. Owner: ui-a.
"""

from __future__ import annotations

from opengrid.ui.templating import templates


def render_status_badge(status: str, *, label: str | None = None) -> str:
    template = templates.env.get_template("_partials/status_badge.html")
    return template.render(status=status, label=label)


def render_stale_badge(
    age_s: float | None,
    *,
    since_iso: str | None = None,
    stale_after_s: float | None = None,
) -> str:
    template = templates.env.get_template("_partials/stale_badge.html")
    return template.render(age_s=age_s, since_iso=since_iso, stale_after_s=stale_after_s)
