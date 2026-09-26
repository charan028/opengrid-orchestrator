"""One alerts component for Control room and System Health (owner UX review, R3). Owner: ui.

- `posture_strip`: the guardian's CURRENT safety posture (`GET /og/api/views/scope-posture`, read from
  `og.scope_posture`, not from open alerts): one compact line while any scope is CONSERVATIVE.
- `alerts_panel`: open alerts grouped by rule + scope (count x N, latest time), filtered by severity and
  rule, newest first, paginated; each group carries the alert ids it stands for, so a row can be
  acknowledged on its own or selected for a bulk acknowledgement.
- `notification_centre`: the header bell's popover (safety, degraded modes, alerts) and the single
  critical status line; nothing stacks above the map any more.

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


#: Degraded modes that are a CRITICAL state (the fleet is held), eligible for the one-line status strip.
CRITICAL_MODES = frozenset({"HOLD", "HOLD_LOCAL_AUTONOMY"})
SAFE_STOP_REQUESTED_RULE = "ALR-SAFE-STOP-REQUESTED"
SCOPE_CONSERVATIVE_RULE = "ALR-SCOPE-CONSERVATIVE"
_NOTIFY_ALERTS_SHOWN = 8


def _top_severity(severities: list[str]) -> str | None:
    return min(severities, key=lambda s: SEVERITY_RANK.get(s, 9)) if severities else None


def notification_centre(
    *,
    guardian_items: list[dict[str, Any]],
    degraded_modes: list[str],
    mode_labels: dict[str, str],
    posture: dict[str, Any] | None,
    alerts: list[dict[str, Any]],
    base_path: str,
) -> dict[str, Any]:
    """The header bell's content (owner UX review, R3.1): every notice that used to stack above the map,
    grouped Safety / Degraded modes / Alerts, plus the ONE critical line allowed at the top of the page
    (an active critical state: a requested safe stop or a fleet hold), or None."""
    health_alerts = f"{base_path}/health#health-alerts-panel"
    safety: list[dict[str, Any]] = [
        {
            "severity": "critical",
            "title": f"{item['title']}{': ' + item['scope_label'] if item.get('scope_label') else ''}",
            "detail": item.get("summary") or "",
            "href": item.get("review_url") or f"{base_path}/fleet",
            "safestop_review": True,
        }
        for item in guardian_items
        if item.get("stop_requested")
    ]
    if posture:
        scopes = ", ".join(posture["shown"]) + (f", +{posture['more']}" if posture.get("more") else "")
        safety.append(
            {
                "severity": "warning",
                "title": f"{posture['count']} scope{'' if posture['count'] == 1 else 's'} held conservative",
                "detail": f"{scopes}. The guardian is vetoing most commands in these scopes; it clears "
                "automatically after 3 clean cycles.",
                "href": f"{base_path}/health?alert_rule={SCOPE_CONSERVATIVE_RULE}#health-alerts-panel",
            }
        )
    degraded = [
        {
            "severity": "critical" if mode in CRITICAL_MODES else "warning",
            "title": f"Degraded mode: {mode_labels.get(mode, mode)}",
            "detail": mode,
            "href": f"{base_path}/health",
        }
        for mode in degraded_modes
    ]
    # Guardian escalations and conservative scopes are already in Safety; the rest are grouped alerts.
    others = [
        a
        for a in alerts
        if isinstance(a, dict) and a.get("rule") not in (SAFE_STOP_REQUESTED_RULE, SCOPE_CONSERVATIVE_RULE)
    ]
    groups = [g for g in alerts_panel(others, size=100)["groups"] if g["unacked_ids"]]
    groups.sort(key=lambda g: SEVERITY_RANK.get(str(g["severity"]), 9))  # stable: newest first per severity
    alert_items = [
        {
            "severity": str(g["severity"]),
            "title": f"{g['rule']}{' on ' + g['scope'] if g['scope'] else ''}",
            "count": g["count"],
            "detail": g["summary"],
            "href": f"{base_path}/health?alert_rule={g['rule']}#health-alerts-panel",
            "ack_ids": g["ids_csv"],
        }
        for g in groups
    ]
    critical = [i for i in (*safety, *degraded) if i["severity"] == "critical"]
    sections = [
        {"key": "safety", "label": "Safety", "entries": safety, "total": len(safety), "more": 0},
        {
            "key": "degraded",
            "label": "Degraded modes",
            "entries": degraded,
            "total": len(degraded),
            "more": 0,
        },
        {
            "key": "alerts",
            "label": "Alerts",
            "entries": alert_items[:_NOTIFY_ALERTS_SHOWN],
            "total": len(alert_items),
            "more": max(len(alert_items) - _NOTIFY_ALERTS_SHOWN, 0),
            "more_href": health_alerts,
        },
    ]
    every = [*safety, *degraded, *alert_items]
    return {
        "count": len(every),
        "top_severity": _top_severity([str(i["severity"]) for i in every]),
        "groups": sections,
        "critical": (
            {"title": critical[0]["title"], "href": critical[0]["href"], "more": len(critical) - 1}
            if critical
            else None
        ),
    }


def parse_ids(raw: str | None) -> list[int]:
    """`"3,5, 9"` -> `[3, 5, 9]` (de-duplicated, order kept); junk is dropped."""
    out: dict[int, None] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if part.isdigit():
            out.setdefault(int(part), None)
    return list(out)
