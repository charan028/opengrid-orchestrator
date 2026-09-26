"""One alerts component for Control room and System Health (owner UX review, R3). Owner: ui.

- `posture_strip`: the guardian's CURRENT safety posture (`GET /og/api/views/scope-posture`, read from
  `og.scope_posture`, not from open alerts): one compact line while any scope is CONSERVATIVE.
- `alerts_panel`: open alerts grouped by rule + scope (count x N, latest time), filtered by severity and
  rule, newest first, paginated; each group carries the alert ids it stands for, so a row can be
  acknowledged on its own or selected for a bulk acknowledgement.

Pure functions; the routes fetch.
"""

from __future__ import annotations

import re
from typing import Any

SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}
PAGE_SIZES = (10, 20, 50, 100)
DEFAULT_PAGE_SIZE = 20
POSTURE_PATH = "/og/api/views/scope-posture"
#: A guardian escalation alert opens with its scope key ("BANK:bank-007: ..."), for payloads without
#: the structured scope fields (before R2's og.alert.scope_kind/scope_ref).
_SUMMARY_SCOPE = re.compile(r"^(BANK|ZONE|HUB):([^:\s]+)[:\s]")
_STRIP_SHOWN = 6


def _scope(alert: dict[str, Any]) -> tuple[str, str]:
    kind, ref = alert.get("scope_kind"), alert.get("scope_ref")
    if kind and ref:
        return str(kind).upper(), str(ref)
    match = _SUMMARY_SCOPE.match(str(alert.get("summary") or ""))
    return (match.group(1), match.group(2)) if match else ("", "")


def posture_strip(payload: Any) -> dict[str, Any] | None:
    """`None` unless some scope is CONSERVATIVE right now. Names the first few scopes, then "+k"."""
    rows = payload.get("conservative") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        return None
    refs = [str(r.get("scope_ref")) for r in rows if r.get("scope_ref")]
    return {
        "count": len(rows),
        "shown": refs[:_STRIP_SHOWN],
        "more": max(len(refs) - _STRIP_SHOWN, 0),
        "stop_requested": sum(1 for r in rows if r.get("stop_requested")),
    }


def _norm_int(value: Any, default: int, allowed: tuple[int, ...] | None = None) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if allowed is not None and n not in allowed:
        return default
    return max(n, 1)


def alerts_panel(
    alerts: list[dict[str, Any]],
    *,
    severity: str | None = None,
    rule: str | None = None,
    page: Any = 1,
    size: Any = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    """Grouped, filtered, sorted, paginated open alerts plus the filter options and the ids behind the
    current filter (for "select all matching")."""
    rows = [a for a in alerts if isinstance(a, dict)]
    rules = sorted({str(a.get("rule")) for a in rows if a.get("rule")})
    severities = sorted(
        {str(a.get("severity")) for a in rows if a.get("severity")}, key=lambda s: SEVERITY_RANK.get(s, 9)
    )
    sev = (severity or "").strip() or None
    rl = (rule or "").strip() or None
    matching = [
        a for a in rows if (sev is None or a.get("severity") == sev) and (rl is None or a.get("rule") == rl)
    ]

    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for a in matching:
        kind, ref = _scope(a)
        key = (str(a.get("rule") or ""), kind, ref, "" if ref else str(a.get("summary") or ""))
        g = groups.get(key)
        opened = str(a.get("opened_at") or "")
        if g is None:
            g = groups[key] = {
                "rule": a.get("rule") or "-",
                "scope": f"{kind.lower()} {ref}" if ref else "",
                "severity": a.get("severity") or "info",
                "summary": a.get("summary") or "",
                "latest": opened,
                "ids": [],
                "unacked_ids": [],
            }
        g["ids"].append(a.get("id"))
        if not a.get("acked_by"):
            g["unacked_ids"].append(a.get("id"))
        if opened >= g["latest"]:
            g["latest"], g["summary"] = opened, a.get("summary") or g["summary"]
        if SEVERITY_RANK.get(str(a.get("severity")), 9) < SEVERITY_RANK.get(str(g["severity"]), 9):
            g["severity"] = a.get("severity")
    ordered = sorted(groups.values(), key=lambda g: g["latest"], reverse=True)
    for g in ordered:
        g["count"] = len(g["ids"])
        g["ids_csv"] = ",".join(str(i) for i in g["unacked_ids"])

    size_n = _norm_int(size, DEFAULT_PAGE_SIZE, PAGE_SIZES)
    pages = max((len(ordered) + size_n - 1) // size_n, 1)
    page_n = min(_norm_int(page, 1), pages)
    start = (page_n - 1) * size_n
    unacked_matching = [a.get("id") for a in matching if not a.get("acked_by")]
    return {
        "groups": ordered[start : start + size_n],
        "group_count": len(ordered),
        "alert_count": len(matching),
        "page": page_n,
        "pages": pages,
        "size": size_n,
        "sizes": PAGE_SIZES,
        "severity": sev or "",
        "rule": rl or "",
        "rules": rules,
        "severities": severities,
        "all_matching_ids_csv": ",".join(str(i) for i in unacked_matching),
        "all_matching_count": len(unacked_matching),
    }


def parse_ids(raw: str | None) -> list[int]:
    """`"3,5, 9"` -> `[3, 5, 9]` (de-duplicated, order kept); junk is dropped."""
    out: dict[int, None] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if part.isdigit():
            out.setdefault(int(part), None)
    return list(out)
