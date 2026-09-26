"""Owner grid-charging windows (OWNER DECISION D-30): the ONE definition of their format and of which
scope's windows apply to a hub or bank. Used by the selector (when grid charging is allowed) and the API
(`GET /og/api/fleet/charge-windows/effective`), so both always agree. Pure: no I/O.

Windows are "HH:MM-HH:MM" strings in America/Chicago local time (`og.owner_charge_window.windows`,
migration 0038). A window may wrap midnight ("22:00-06:00" = 22:00 to 06:00 next day); its end is
exclusive. At most `MAX_WINDOWS` per scope; an empty list is a valid override meaning "no grid charging".

Precedence, least to most specific -- the most specific scope that has a row wins:
    FLEET < PROVIDER < ZONE < SUBSTATION < FEEDER < BANK < HUB
PROVIDER is the zone's regulated utility (e.g. AUSTIN_ENERGY) or, in a competitive zone, its TDSP (e.g.
ONCOR, CENTERPOINT) -- the caller resolves it from the market configuration.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import time
from typing import Literal

ScopeKind = Literal["FLEET", "PROVIDER", "ZONE", "SUBSTATION", "FEEDER", "BANK", "HUB"]

#: Least to most specific.
SCOPE_PRECEDENCE: tuple[ScopeKind, ...] = ("FLEET", "PROVIDER", "ZONE", "SUBSTATION", "FEEDER", "BANK", "HUB")
FLEET_REF = "*"
MAX_WINDOWS = 4
CHARGE_WINDOW_TZ = "America/Chicago"

_WINDOW_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")


class ChargeWindowError(ValueError):
    """A window list or scope that does not meet the D-30 format."""


def parse_window(text: str) -> tuple[time, time]:
    """`"HH:MM-HH:MM"` -> `(start, end)`. Raises `ChargeWindowError` on a bad format or a zero-length
    window (start == end)."""
    match = _WINDOW_RE.match(text.strip())
    if match is None:
        raise ChargeWindowError(f"window {text!r} is not HH:MM-HH:MM (24 h)")
    start = time(int(match.group(1)), int(match.group(2)))
    end = time(int(match.group(3)), int(match.group(4)))
    if start == end:
        raise ChargeWindowError(f"window {text!r} has no length")
    return start, end


def validate_windows(windows: Sequence[str]) -> list[str]:
    """The windows normalised (stripped), or `ChargeWindowError` for a bad entry, more than
    `MAX_WINDOWS`, or a duplicate. Overlaps are allowed (the union applies)."""
    if len(windows) > MAX_WINDOWS:
        raise ChargeWindowError(f"at most {MAX_WINDOWS} windows per scope")
    normalised = [w.strip() for w in windows]
    for window in normalised:
        parse_window(window)
    if len(set(normalised)) != len(normalised):
        raise ChargeWindowError("duplicate window")
    return normalised


def validate_scope(scope_kind: str, scope_ref: str) -> ScopeKind:
    """The scope kind, checked: one of `SCOPE_PRECEDENCE`; FLEET's ref must be `FLEET_REF`, every other
    ref non-empty."""
    if scope_kind not in SCOPE_PRECEDENCE:
        raise ChargeWindowError(f"scope_kind must be one of {', '.join(SCOPE_PRECEDENCE)}")
    if scope_kind == "FLEET" and scope_ref != FLEET_REF:
        raise ChargeWindowError(f"the FLEET scope_ref is {FLEET_REF!r}")
    if not scope_ref.strip():
        raise ChargeWindowError("scope_ref is required")
    return scope_kind


def in_windows(windows: Sequence[str], local: time) -> bool:
    """Whether local wall-clock `local` (America/Chicago) falls inside any window (end exclusive)."""
    for window in windows:
        start, end = parse_window(window)
        inside = start <= local < end if start < end else (local >= start or local < end)
        if inside:
            return True
    return False


def provider_for_zone(
    zone: str | None, zone_utility: Mapping[str, str], zone_default_tdsp: Mapping[str, str]
) -> str | None:
    """The PROVIDER scope of a load zone: its regulated utility / NOIE owner when it has one
    (`tdsp_tariffs.toml` `[zone_territory]`), else its competitive-area TDSP (`[zone_default_tdsp]`),
    else None."""
    if zone is None:
        return None
    return zone_utility.get(zone) or zone_default_tdsp.get(zone)


@dataclass(frozen=True, slots=True)
class Topology:
    """Where a hub (or bank) sits, for resolution. Unknown levels are None and simply skipped."""

    hub_id: str | None = None
    bank_id: str | None = None
    feeder_id: str | None = None
    substation_id: str | None = None
    zone: str | None = None
    provider: str | None = None

    def chain(self) -> list[tuple[ScopeKind, str]]:
        """The scopes that could apply, most specific first, ending with FLEET."""
        levels: list[tuple[ScopeKind, str | None]] = [
            ("HUB", self.hub_id),
            ("BANK", self.bank_id),
            ("FEEDER", self.feeder_id),
            ("SUBSTATION", self.substation_id),
            ("ZONE", self.zone),
            ("PROVIDER", self.provider),
            ("FLEET", FLEET_REF),
        ]
        return [(kind, ref) for kind, ref in levels if ref]


@dataclass(frozen=True, slots=True)
class EffectiveWindows:
    windows: tuple[str, ...]
    scope_kind: ScopeKind
    scope_ref: str


def resolve(rows: Mapping[tuple[str, str], Sequence[str]], topology: Topology) -> EffectiveWindows | None:
    """The windows that apply at `topology`: the most specific scope with a row in `rows`
    (`{(scope_kind, scope_ref): windows}`, i.e. `og.owner_charge_window`). None only when not even a
    FLEET row exists (callers then treat grid charging as unrestricted or refuse -- their policy)."""
    for kind, ref in topology.chain():
        windows = rows.get((kind, ref))
        if windows is not None:
            return EffectiveWindows(windows=tuple(windows), scope_kind=kind, scope_ref=ref)
    return None
