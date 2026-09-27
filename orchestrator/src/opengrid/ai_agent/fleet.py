"""Fleet questions: "how many units have capacity 78.4 kWh", "total available kW in LZ_AEN". Owner: ui-a/ai.

The copilot answers these from a read-only fleet tool (issue #26 follow-up): the question becomes a
`FleetQuery` (closed vocabulary, typed values), the API runs it through the Fleet table's own filter and
aggregate layer (`opengrid.api.routers.fleet_search`) with every value bound as a parameter, and this
module writes the answer from the tool's numbers. No model writes a fleet number: the text is rendered
here, so every count and total in it is the tool's.

Two ways a question becomes a `FleetQuery`:

* `parse` -- deterministic, no model. It recognises the owner's phrasings (capacity, power, charge,
  zone, bank, availability, health, firmware/hardware, asset class, trucks at home, "by zone" breakdowns,
  available kW/kWh) and returns None for anything it is unsure of;
* `from_model` -- the routing model's structured extraction (a strict tool schema), validated here
  value by value. A value outside the vocabulary is dropped, never passed on.

Pure: no I/O. The tool itself is injected by the API router (`types.FleetTool`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from opengrid.ai_agent.types import Citation, CopilotAnswer, FleetQuery

#: Where the numbers come from: the Fleet summary endpoint the tool shares with the console.
SUMMARY_SOURCE = "/og/api/fleet/summary"
#: How many hub rows an answer names (the tool returns at most this many, too).
TOP_ROWS = 5

_ZONE = re.compile(r"\bLZ[_ ]([A-Za-z]{2,12})\b", re.IGNORECASE)
_ZONE_VALUE = re.compile(r"^LZ_[A-Z]{2,12}$")
_ID_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_NUMBER = r"(\d+(?:\.\d+)?)"
_BELOW = r"(below|under|less than|lower than|fewer than|at most|no more than|<=?)"
_ABOVE = r"(above|over|more than|greater than|higher than|at least|no less than|>=?)"
#: Unit spellings: "kWh" / "kilowatt hours", "kW" / "kilowatts" (never followed by "hours"), "%" / "percent".
_KWH = r"(?:kwh|kilowatt[- ]?hours?)\b"
_KW = r"(?:kw|kilowatts?)\b(?![- ]?hours?)"
_PCT = r"(?:%|\s*(?:percent|pct)\b)"
#: FleetQuery field prefix -> the unit that marks it.
_RATINGS: tuple[tuple[str, str, str], ...] = (("capacity", "kwh", _KWH), ("power", "kw", _KW))

#: A condition word left over after every recognised phrase is taken out means the parser did NOT
#: understand part of the question. Comparators, numbers and units are conditions on their own; a topic
#: word ("capacity", "charge") only counts when no filter on that topic was parsed.
_CONDITION_CUE = re.compile(
    r"\d|%|\b(more|less|fewer|greater|higher|lower|above|below|over|under|at least|at most|between|exceed\w*"
    r"|than|kwh|kw|mwh|mw|kilowatts?|megawatts?|watts?|percent|pct|full|empty|low|high|largest|smallest"
    r"|biggest|top|bottom|most|least)\b",
    re.IGNORECASE,
)
#: Topic word -> the FleetQuery fields that answer it (any one of them is enough).
_TOPICS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"\bcapacity\b", re.I), ("capacity_min_kwh", "capacity_max_kwh")),
    (re.compile(r"\b(charge[d]?|soc|state of charge)\b", re.I), ("soc_min_pct", "soc_max_pct")),
    (re.compile(r"\bpower\b", re.I), ("power_min_kw", "power_max_kw")),
    (
        re.compile(r"\brated\b", re.I),
        ("capacity_min_kwh", "capacity_max_kwh", "power_min_kw", "power_max_kw"),
    ),
    (re.compile(r"\b(firmware|version)\b", re.I), ("fw",)),
    (re.compile(r"\bhardware\b", re.I), ("hw",)),
    (re.compile(r"\bzones?\b", re.I), ("zones", "group_by")),
    (re.compile(r"\bbank\b", re.I), ("bank",)),
)
#: Which filters could answer a bare comparator/number/unit cue, by the unit it names.
_CUE_FIELDS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"kwh|kilowatt[- ]?hours?|mwh|capacity", re.I), ("capacity_min_kwh", "capacity_max_kwh")),
    (re.compile(r"\b(kw|kilowatts?|mw|megawatts?|watts?|power)\b", re.I), ("power_min_kw", "power_max_kw")),
    (re.compile(r"%|percent|pct|charge|soc|full|empty", re.I), ("soc_min_pct", "soc_max_pct")),
)
_RATING_FIELDS = (
    "capacity_min_kwh",
    "capacity_max_kwh",
    "power_min_kw",
    "power_max_kw",
    "soc_min_pct",
    "soc_max_pct",
)


@dataclass(frozen=True, slots=True)
class UnparsedCondition:
    """A fleet question with a condition the parser could not read. It is never answered with a count
    unless the routing model's validated filters cover it (`covers`); otherwise the operator is told
    which words were not understood (`not_understood`)."""

    text: str
    #: FleetQuery fields any one of which would answer the condition (empty: none can be vouched for).
    fields: tuple[str, ...]


_FLEET_NOUN = re.compile(
    r"\b(units?|hubs?|batter(?:y|ies)|trucks?|substations?|homes?|fleet|banks?|trailers?)\b", re.IGNORECASE
)
#: Questions another handler owns (obligations, alerts, promises) or that ask for a reason.
_NOT_FLEET = re.compile(
    r"\b(why|obligations?|alerts?|alarms?|at risk|offers?|breach|promises?|invoice|contract price)\b",
    re.IGNORECASE,
)
_AGGREGATE_CUE = re.compile(r"\b(how many|number of|count|total|sum|breakdown)\b", re.IGNORECASE)
_AT_HOME = re.compile(r"\bat (?:their |its |the )?(?:home(?: station)?|depot|base)\b", re.IGNORECASE)
_AWAY = re.compile(
    r"\b(away from (?:home|(?:their |its |the )?(?:home station|depot))|not at home|deployed)\b", re.I
)

_ASSET_WORDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("truck", re.compile(r"\b(trucks?|mobile(?: units?| storage)?|trailers?)\b", re.IGNORECASE)),
    ("substation", re.compile(r"\b(substations?|utility[- ]scale)\b", re.IGNORECASE)),
    ("dual_unit", re.compile(r"\b(dual[- ]unit|two[- ]unit|2[- ]unit|double[- ]unit)\b", re.IGNORECASE)),
    (
        "home",
        re.compile(r"\b(home batter(?:y|ies)|home units?|homes|residential|home hubs?)\b", re.IGNORECASE),
    ),
)
_HEALTH_WORDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("online", re.compile(r"\b(online|healthy)\b", re.IGNORECASE)),
    ("stale", re.compile(r"\b(stale|watch)\b", re.IGNORECASE)),
    ("offline", re.compile(r"\b(offline|not reporting|dark)\b", re.IGNORECASE)),
    ("fault", re.compile(r"\b(fault(?:ed|y|s)?|in fault)\b", re.IGNORECASE)),
    ("degraded", re.compile(r"\bdegraded\b", re.IGNORECASE)),
    ("quarantined", re.compile(r"\bquarantined?\b", re.IGNORECASE)),
)
#: "Health issues/problems/risks" means every state other than online.
_UNHEALTHY = re.compile(
    r"\b(health (?:issues?|problems?|risks?)|unhealthy|low health|not healthy|problem (?:units|hubs))\b",
    re.IGNORECASE,
)
_NOT_ONLINE: tuple[str, ...] = ("stale", "degraded", "quarantined", "fault", "offline")
_UNAVAILABLE = re.compile(
    r"\b(unavailable|not available|regulated|no contract|regulated_no_contract)\b", re.I
)
_AVAILABLE_NOUN = re.compile(r"\b(available (?:hubs|units|banks|batteries)|(?:are|is) available)\b", re.I)
_BANK = re.compile(r"\b(bank-[A-Za-z0-9][A-Za-z0-9._-]*)\b")
_FIRMWARE = re.compile(r"\bfirmware\s+(?:version\s+)?v?([A-Za-z0-9][A-Za-z0-9._-]*)", re.IGNORECASE)
_HARDWARE = re.compile(r"\bhardware\s+(?:revision\s+|rev\s+)?([A-Za-z0-9][A-Za-z0-9._-]*)", re.IGNORECASE)
_METRICS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("available_kwh", re.compile(r"\b(available\s+(?:kwh|energy)|kwh\s+available)\b", re.IGNORECASE)),
    ("available_kw", re.compile(r"\b(available\s+(?:kw|power|capacity)|kw\s+available)\b", re.IGNORECASE)),
    ("rated_kwh", re.compile(r"\btotal\s+(?:rated\s+)?(?:kwh|capacity|energy)\b", re.IGNORECASE)),
    ("rated_kw", re.compile(r"\btotal\s+(?:rated\s+)?(?:kw|power)\b", re.IGNORECASE)),
)
_GROUPS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("zone", re.compile(r"\b(?:by|per|each|every)\s+(?:load\s+)?zones?\b", re.IGNORECASE)),
    ("availability", re.compile(r"\b(?:by|per)\s+availability\b", re.IGNORECASE)),
    (
        "soc_bucket",
        re.compile(r"\b(?:by|per)\s+(?:soc|charge|state of charge)(?:\s+(?:bucket|level|band)s?)?\b", re.I),
    ),
    ("health", re.compile(r"\b(?:by|per)\s+(?:health|status)\b", re.IGNORECASE)),
    ("asset_class", re.compile(r"\b(?:by|per)\s+(?:asset\s+)?(?:class|type)\b", re.IGNORECASE)),
)


Spans = list[tuple[int, int]]


def _search(pattern: re.Pattern[str], text: str, spans: Spans) -> re.Match[str] | None:
    """`pattern.search`, recording the matched span as understood."""
    match = pattern.search(text)
    if match:
        spans.append(match.span())
    return match


def _bounds(question: str, unit: str, spans: Spans, *, exact: bool) -> tuple[float | None, float | None]:
    """(min, max) for `<comparator> <number> <unit>` mentions ("more than 40 kWh", "below 30%",
    "between 20 and 40 kW"). With `exact`, a bare number and unit is an exact value (min == max; the API
    widens it by a rounding tolerance); without it (SoC), a bare percentage is not a condition we read."""
    low: float | None = None
    high: float | None = None
    between = re.search(
        rf"\bbetween\s+{_NUMBER}\s*(?:{unit})?\s+and\s+{_NUMBER}\s*{unit}", question, re.IGNORECASE
    )
    if between:
        spans.append(between.span())
        a, b = sorted((float(between.group(1)), float(between.group(2))))
        return a, b
    comparator = "?" if exact else ""
    for match in re.finditer(rf"(?:{_BELOW}|{_ABOVE}){comparator}\s*{_NUMBER}\s*{unit}", question, re.I):
        spans.append(match.span())
        value = float(match.group(3))
        if match.group(1):
            high = value
        elif match.group(2):
            low = value
        else:
            low = high = value
    return low, high


def _masked(text: str, spans: Spans) -> str:
    """`text` with every understood span blanked out (positions kept)."""
    chars = list(text)
    for start, end in spans:
        chars[start:end] = " " * (end - start)
    return "".join(chars)


def _unparsed(text: str, spans: Spans, fields: dict[str, Any]) -> UnparsedCondition | None:
    """The condition the parser did not understand, if any: a leftover comparator, number or unit, or a
    topic word ("capacity", "charge") with no filter on that topic."""
    rest = _masked(text, spans)
    cue = _CONDITION_CUE.search(rest)
    start: int | None = cue.start() if cue else None
    answers: tuple[str, ...] = ()
    if cue is not None:
        tail = rest[cue.start() :]
        answers = tuple(f for pattern, names in _CUE_FIELDS if pattern.search(tail) for f in names)
        if not answers:
            answers = _RATING_FIELDS if re.search(r"\d", tail) else ()
    for pattern, names in _TOPICS:
        topic = pattern.search(rest)
        if topic and not any(fields.get(name) not in (None, (), "none") for name in names):
            start = topic.start() if start is None else min(start, topic.start())
            answers = tuple(dict.fromkeys((*answers, *names)))
    if start is None:
        return None
    snippet = re.split(r"[?.!;]", text[start:], maxsplit=1)[0].strip()[:80]
    return UnparsedCondition(text=snippet, fields=answers)


_SMALL_NUMBERS = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_NUMBER_WORDS: dict[str, int] = {
    **{word: value for value, word in enumerate(_SMALL_NUMBERS)},
    **{
        w: 10 * (i + 2)
        for i, w in enumerate(["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"])
    },
}
_NUMBER_WORD_RUN = re.compile(
    r"\b(?:"
    + "|".join([*_NUMBER_WORDS, "hundred", "thousand"])
    + r")(?:[\s-]+(?:and[\s-]+)?(?:"
    + "|".join([*_NUMBER_WORDS, "hundred", "thousand"])
    + r"))*\b",
    re.IGNORECASE,
)


def _number_value(words: str) -> int:
    total = current = 0
    for word in re.split(r"[\s-]+", words.lower()):
        if word == "and":
            continue
        if word == "hundred":
            current = max(current, 1) * 100
        elif word == "thousand":
            total += max(current, 1) * 1000
            current = 0
        else:
            current += _NUMBER_WORDS[word]
    return total + current


def spell_numbers(text: str) -> str:
    """Number words as digits ("more than forty kWh" -> "more than 40 kWh", "one hundred twenty" -> "120"),
    so a condition written in words is read by the same rules as one written in digits -- on every path."""
    return _NUMBER_WORD_RUN.sub(lambda m: str(_number_value(m.group(0))), text)


#: Words a fleet question may contain besides the phrases the parser understood. Anything else left over
#: ("in Houston", "near the depot", "a lot of") may be a condition the parser did not read, so the question
#: is refused rather than answered with a count that ignores it.
_FILLER = frozenset(
    [
        "a",
        "all",
        "an",
        "and",
        "any",
        "are",
        "at",
        "be",
        "being",
        "battery",
        "batteries",
        "by",
        "can",
        "count",
        "currently",
        "do",
        "does",
        "each",
        "entire",
        "exist",
        "fleet",
        "for",
        "give",
        "got",
        "has",
        "have",
        "hub",
        "hubs",
        "i",
        "in",
        "is",
        "issues",
        "it",
        "its",
        "list",
        "many",
        "me",
        "much",
        "my",
        "now",
        "number",
        "of",
        "on",
        "or",
        "our",
        "overall",
        "please",
        "problems",
        "right",
        "s",
        "score",
        "show",
        "sum",
        "tell",
        "that",
        "the",
        "their",
        "there",
        "these",
        "those",
        "today",
        "total",
        "unit",
        "units",
        "we",
        "what",
        "whats",
        "which",
        "whole",
        "with",
        "you",
        "homes",
        "home",
        "trucks",
        "truck",
        "banks",
        "bank",
        "substations",
        "substation",
        "capacity",
        "charge",
        "charged",
        "soc",
        "state",
        "power",
        "rated",
        "zone",
        "zones",
        "firmware",
        "version",
        "hardware",
        "how",
        "altogether",
        "currently",
    ]
)


def _leftover_words(text: str, spans: Spans) -> list[str]:
    return [w for w in re.findall(r"[a-z]+", _masked(text, spans).lower()) if w not in _FILLER]


def parse(question: str) -> FleetQuery | UnparsedCondition | None:
    """The fleet query a question asks for; `UnparsedCondition` when it is a fleet question with a
    condition the parser could not read (never answered as an unfiltered count); or None when it is not
    (clearly) a fleet aggregate question.

    Conservative on purpose: a question that also names obligations, alerts or a reason ("why") is left
    to the handlers that own those, and a fleet noun with no filter, grouping, metric or counting cue
    ("tell me about the fleet") is left to the health summary. Every phrase that sets a filter is recorded
    as understood; whatever condition word is left over makes the question unparsed."""
    text = spell_numbers(question.strip())
    if not text or _NOT_FLEET.search(text):
        return None
    spans: Spans = []
    fields: dict[str, Any] = {}
    zone_matches = list(_ZONE.finditer(text))
    spans.extend(m.span() for m in zone_matches)
    zones = tuple(dict.fromkeys(f"LZ_{m.group(1).upper()}" for m in zone_matches))
    if zones:
        fields["zones"] = zones
    at_home: bool | None = None
    if _search(_AWAY, text, spans):
        at_home = False
    elif _search(_AT_HOME, text, spans):
        at_home = True
    # "trucks at home" is about location, not the home asset class: drop the phrase before matching.
    classifiable = _masked(text, spans)
    asset = next((name for name, pattern in _ASSET_WORDS if _search(pattern, classifiable, spans)), None)
    if at_home is not None:
        asset = "truck"
        fields["at_home"] = at_home
    if asset:
        fields["asset_class"] = asset
    if _search(_UNHEALTHY, text, spans):
        fields["health"] = _NOT_ONLINE
    else:
        health = tuple(name for name, pattern in _HEALTH_WORDS if _search(pattern, text, spans))
        if health:
            fields["health"] = health
    if _search(_UNAVAILABLE, text, spans):
        fields["availability"] = "UNAVAILABLE"
    elif _search(_AVAILABLE_NOUN, text, spans):
        fields["availability"] = "AVAILABLE"
    for prefix, suffix, unit in _RATINGS:
        low, high = _bounds(text, unit, spans, exact=True)
        if low is not None:
            fields[f"{prefix}_min_{suffix}"] = low
        if high is not None:
            fields[f"{prefix}_max_{suffix}"] = high
    soc_min, soc_max = _bounds(text, _PCT, spans, exact=False)
    if soc_min is not None:
        fields["soc_min_pct"] = soc_min
    if soc_max is not None:
        fields["soc_max_pct"] = soc_max
    for key, pattern in (("bank", _BANK), ("fw", _FIRMWARE), ("hw", _HARDWARE)):
        found = _search(pattern, text, spans)
        if found:
            fields[key] = found.group(1)
    metric = next((name for name, pattern in _METRICS if _search(pattern, text, spans)), None)
    if metric:
        fields["metric"] = metric
    group = next((name for name, pattern in _GROUPS if _search(pattern, text, spans)), None)
    if group:
        fields["group_by"] = group
    if not (_FLEET_NOUN.search(text) or metric):
        return None  # "how many are below 30%?" of what? Not a fleet question we can vouch for.
    unparsed = _unparsed(text, spans, fields)
    if unparsed is not None:
        return unparsed
    query = FleetQuery.model_validate(fields)
    if not (query.has_filter or metric or group or _AGGREGATE_CUE.search(text)):
        return None
    leftover = _leftover_words(text, spans)
    if leftover:
        # e.g. "how many hubs in Houston": an unread word may be a filter; never answer the whole fleet.
        return UnparsedCondition(text=" ".join(leftover)[:80], fields=())
    return query


def not_understood(condition: UnparsedCondition) -> CopilotAnswer:
    """The answer when a condition was not understood: what was not understood and how to phrase it,
    and no count at all (an unfiltered count would answer a different question)."""
    return CopilotAnswer(
        text=(
            f"I couldn't understand the condition '{condition.text}', so nothing was counted. Write it "
            "with a number and a unit, for example 'more than N kWh', 'at least N kW', 'below N% charge' "
            "or 'between N% and M% charge'."
        ),
        tier="deterministic",
        intent="fleet_query",
        refusal_reason="fleet_condition_unparsed",
    )


def from_model(raw: Any) -> FleetQuery | None:
    """The routing model's extracted filters as a `FleetQuery`, or None. Each value is checked against
    the closed vocabulary; zone, bank and version strings must look like identifiers. Anything else is
    dropped (fail closed), so model output can never widen what the tool is asked."""
    if not isinstance(raw, dict):
        return None
    cleaned: dict[str, Any] = {}
    zones = raw.get("zones")
    if isinstance(zones, list):
        valid = [str(z).upper() for z in zones[:10] if _ZONE_VALUE.match(str(z).upper())]
        if valid:
            cleaned["zones"] = tuple(dict.fromkeys(valid))
    for key in ("bank", "hw", "fw"):
        value = raw.get(key)
        if isinstance(value, str) and _ID_VALUE.match(value):
            cleaned[key] = value
    for key in (
        "soc_min_pct",
        "soc_max_pct",
        "capacity_min_kwh",
        "capacity_max_kwh",
        "power_min_kw",
        "power_max_kw",
    ):
        value = raw.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool) and 0 <= value <= 1e7:
            cleaned[key] = float(value)
    if isinstance(raw.get("at_home"), bool):
        cleaned["at_home"] = raw["at_home"]
    health = raw.get("health")
    if isinstance(health, list):
        cleaned["health"] = tuple(dict.fromkeys(str(h) for h in health[:6]))
    for key in ("asset_class", "availability", "group_by", "metric"):
        if isinstance(raw.get(key), str):
            cleaned[key] = raw[key]
    # Validate field by field: one bad enum value drops that field, not the whole query.
    accepted: dict[str, Any] = {}
    for key, value in cleaned.items():
        try:
            FleetQuery.model_validate({key: value})
        except ValidationError:
            continue
        accepted[key] = value
    if not accepted:
        return None
    return FleetQuery.model_validate(accepted)


# -- the answer -----------------------------------------------------------------------------------------

_NOUNS = {
    None: "hubs",
    "home": "home hubs",
    "dual_unit": "dual-unit homes",
    "substation": "substation hubs",
    "truck": "trucks",
}


def _num(value: float) -> str:
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.1f}"


def _range(low: float | None, high: float | None, unit: str) -> str | None:
    if low is None and high is None:
        return None
    if low is not None and high is not None:
        return f"{_num(low)} {unit}" if low == high else f"{_num(low)}-{_num(high)} {unit}"
    return f"at least {_num(low or 0)} {unit}" if low is not None else f"at most {_num(high or 0)} {unit}"


def describe(query: FleetQuery) -> str:
    """The filters in plain words, e.g. "dual-unit homes in LZ_NORTH at or below 30% charge"."""
    parts: list[str] = [_NOUNS.get(query.asset_class, "hubs")]
    capacity = _range(query.capacity_min_kwh, query.capacity_max_kwh, "kWh")
    if capacity:
        parts.append(f"rated {capacity}")
    power = _range(query.power_min_kw, query.power_max_kw, "kW")
    if power:
        parts.append(f"rated {power}")
    if query.zones:
        parts.append("in " + ", ".join(query.zones))
    if query.bank:
        parts.append(f"in bank {query.bank}")
    if query.soc_min_pct is not None and query.soc_max_pct is not None:
        parts.append(f"between {_num(query.soc_min_pct)}% and {_num(query.soc_max_pct)}% charge")
    elif query.soc_max_pct is not None:
        parts.append(f"at or below {_num(query.soc_max_pct)}% charge")
    elif query.soc_min_pct is not None:
        parts.append(f"at or above {_num(query.soc_min_pct)}% charge")
    if query.health:
        parts.append("that are " + " or ".join(query.health))
    if query.availability == "UNAVAILABLE":
        parts.append("on unavailable banks (regulated market, no contract)")
    elif query.availability == "AVAILABLE":
        parts.append("on available banks")
    if query.hw:
        parts.append(f"on hardware {query.hw}")
    if query.fw:
        parts.append(f"on firmware {query.fw}")
    return " ".join(parts)


_METRIC_TEXT = {
    "available_kw": ("kW available now", "available_kw"),
    "available_kwh": ("kWh above reserve available now", "available_kwh"),
    "rated_kw": ("kW rated", "rated_kw"),
    "rated_kwh": ("kWh rated", "rated_kwh"),
    "count": ("", "hubs"),
}


def _citations(query: FleetQuery, result: dict[str, Any]) -> list[Citation]:
    citations = [
        Citation(
            source=SUMMARY_SOURCE,
            ref=query.model_dump_json(exclude_defaults=True),
            label=f"fleet summary: {describe(query)}",
        )
    ]
    for row in list(result.get("rows") or [])[:TOP_ROWS]:
        hub_id = str(row.get("hub_id", "?"))
        citations.append(Citation(source=f"/og/api/fleet/hubs/{hub_id}/detail", ref=hub_id, label=hub_id))
    return citations


def _examples(result: dict[str, Any]) -> str:
    rows = list(result.get("rows") or [])[:TOP_ROWS]
    if not rows:
        return ""
    shown = []
    for row in rows:
        detail = [str(row.get("zone") or "")]
        if row.get("soc_pct") is not None:
            detail.append(f"{_num(float(row['soc_pct']))}% charge")
        if row.get("health"):
            detail.append(str(row["health"]))
        shown.append(f"{row.get('hub_id')} ({', '.join(d for d in detail if d)})")
    return " For example: " + "; ".join(shown) + "."


def _mobile_text(query: FleetQuery, mobile: dict[str, Any]) -> str:
    total = int(mobile.get("units", 0))
    home, away, unknown = (
        int(mobile.get("at_home", 0)),
        int(mobile.get("away", 0)),
        int(mobile.get("unknown", 0)),
    )
    where = describe(query.model_copy(update={"asset_class": "truck"}))
    if query.at_home is False:
        ids = ", ".join(mobile.get("away_ids") or [])
        head = f"{away} of {total} {where} are away from their home station"
    else:
        ids = ", ".join(mobile.get("at_home_ids") or [])
        head = f"{home} of {total} {where} are at their home station"
    text = head + (f": {ids}." if ids else ".")
    other = f"{away} away" if query.at_home is not False else f"{home} at home"
    text += f" The rest: {other}"
    if unknown:
        text += f", {unknown} with no known position (treated as away, so not charged)"
    return text + "."


def render(query: FleetQuery, result: dict[str, Any]) -> CopilotAnswer:
    """The answer, written from the tool's numbers only. Deterministic tier: no model wrote this text."""
    mobile = result.get("mobile")
    if isinstance(mobile, dict) and (query.at_home is not None or query.asset_class == "truck"):
        text = _mobile_text(query, mobile)
        if query.at_home is None:
            matched = dict(result.get("total") or {})
            text = f"{_num(float(matched.get('hubs', 0)))} {describe(query)}. " + text
        return CopilotAnswer(
            text=text, tier="deterministic", intent="fleet_query", citations=_citations(query, result)
        )
    total: dict[str, Any] = dict(result.get("total") or {})
    hubs = int(total.get("hubs", 0))
    what = describe(query)
    label, field = _METRIC_TEXT[query.metric]
    if hubs == 0:
        text = f"No {what} right now."
    elif query.metric == "count":
        text = f"{_num(hubs)} {what}."
    elif query.metric.startswith("available"):
        text = (
            f"{_num(float(total.get(field, 0)))} {label} from {_num(int(total.get('available_hubs', 0)))} of "
            f"{_num(hubs)} {what} (online or stale, on an available bank). Rated in total: "
            f"{_num(float(total.get('rated_kw', 0)))} kW, {_num(float(total.get('rated_kwh', 0)))} kWh."
        )
    else:
        text = f"{_num(float(total.get(field, 0)))} {label} across {_num(hubs)} {what}."
    groups = list(result.get("groups") or [])
    if groups:
        unit = "%" if query.group_by == "soc_bucket" else ""
        shown = ", ".join(f"{g.get('key')}{unit} {_num(float(g.get(field, 0)))}" for g in groups)
        noun = "hubs" if query.metric == "count" else label
        text += f" By {query.group_by.replace('_', ' ')} ({noun}): {shown}."
    else:
        text += _examples(result)  # a breakdown is the answer; sample rows would only add noise
    return CopilotAnswer(
        text=text, tier="deterministic", intent="fleet_query", citations=_citations(query, result)
    )


def unavailable() -> CopilotAnswer:
    """The honest answer when the fleet read failed: never a count of zero."""
    return CopilotAnswer(
        text="Can't verify right now: fleet data unavailable.",
        tier="deterministic",
        intent="fleet_query",
        refusal_reason="fleet_unavailable",
        citations=[Citation(source=SUMMARY_SOURCE, ref="fleet_unavailable", label="fleet data unavailable")],
    )
