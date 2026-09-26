"""IEEE 2030.5-2018 (SEP 2.0) resource parsing for the DER control function set (protocol-adapters.md S4).

Only the resources the DER-control client walks are modelled:

    DeviceCapability (/dcap) -> EndDeviceList -> EndDevice -> FunctionSetAssignmentsList
      -> DERProgramList (by primacy) -> DERControlList + DefaultDERControl

All XML is parsed with `defusedxml` (no entity expansion, no external resolution). Namespace:
`urn:ieee:std:2030.5:ns`; media type `application/sep+xml`. Times are Unix seconds (UTC).
Units follow the standard: `opModMaxLimW` / `opModFixedW` are (Signed)PerCent in hundredths of a
percent of the DER's `setMaxW`; `opModTargetW` is an ActivePower `value * 10^multiplier` watts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from xml.etree.ElementTree import Element
from xml.sax.saxutils import escape

from defusedxml.ElementTree import fromstring

__all__ = [
    "NS",
    "SEP_MEDIA_TYPE",
    "DerControl",
    "DerControlBase",
    "DerProgram",
    "EndDevice",
    "Link",
    "build_der_control_response",
    "parse_default_der_control",
    "parse_der_control_list",
    "parse_der_program_list",
    "parse_device_capability",
    "parse_end_device_list",
    "parse_fsa_list",
]

NS = "urn:ieee:std:2030.5:ns"
SEP_MEDIA_TYPE = "application/sep+xml"
_P = f"{{{NS}}}"

# EventStatus.currentStatus (2030.5 Table 27)
EVENT_SCHEDULED = 0
EVENT_ACTIVE = 1
EVENT_CANCELLED = 2
EVENT_CANCELLED_RANDOM = 3
EVENT_SUPERSEDED = 4


class Sep2ParseError(ValueError):
    """A resource that is not well-formed 2030.5 XML."""


@dataclass(frozen=True, slots=True)
class Link:
    href: str


@dataclass(frozen=True, slots=True)
class EndDevice:
    href: str
    lfdi: str | None
    sfdi: int | None
    fsa_list: Link | None


@dataclass(frozen=True, slots=True)
class DerProgram:
    href: str
    mrid: str
    primacy: int
    description: str
    control_list: Link | None
    default_control: Link | None


@dataclass(frozen=True, slots=True)
class DerControlBase:
    op_mod_connect: bool | None = None
    op_mod_energize: bool | None = None
    op_mod_max_lim_w_pct: float | None = None  # percent of setMaxW
    op_mod_fixed_w_pct: float | None = None  # signed percent of setMaxW
    op_mod_target_w: float | None = None  # watts


@dataclass(frozen=True, slots=True)
class DerControl:
    href: str | None
    mrid: str
    description: str
    creation_time: datetime | None
    current_status: int
    start: datetime
    duration_s: int
    base: DerControlBase
    reply_to: str | None = None
    response_required: int = 0
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def end(self) -> datetime:
        return datetime.fromtimestamp(self.start.timestamp() + self.duration_s, tz=UTC)

    def is_active(self, now: datetime) -> bool:
        if self.current_status in (EVENT_CANCELLED, EVENT_CANCELLED_RANDOM, EVENT_SUPERSEDED):
            return False
        return self.start <= now < self.end


# -- helpers -----------------------------------------------------------------------------------------


def _root(xml: bytes | str, expected: str) -> Element:
    try:
        root = fromstring(xml)
    except Exception as exc:  # defusedxml raises several exception types (ParseError, EntitiesForbidden)
        raise Sep2ParseError(f"invalid 2030.5 XML: {exc}") from exc
    if root.tag != f"{_P}{expected}":
        raise Sep2ParseError(f"expected <{expected}> in namespace {NS}, got {root.tag}")
    return root


def _text(el: Element, name: str) -> str | None:
    child = el.find(f"{_P}{name}")
    return child.text.strip() if child is not None and child.text is not None else None


def _link(el: Element, name: str) -> Link | None:
    child = el.find(f"{_P}{name}")
    if child is None or not child.get("href"):
        return None
    return Link(href=str(child.get("href")))


def _bool(value: str | None) -> bool | None:
    if value is None:
        return None
    return value.lower() in ("true", "1")


def _ts(value: str | None) -> datetime | None:
    return datetime.fromtimestamp(int(value), tz=UTC) if value is not None else None


# -- parsers -----------------------------------------------------------------------------------------


def parse_device_capability(xml: bytes | str) -> Link:
    """The EndDeviceListLink of a DeviceCapability resource."""
    root = _root(xml, "DeviceCapability")
    link = _link(root, "EndDeviceListLink")
    if link is None:
        raise Sep2ParseError("DeviceCapability has no EndDeviceListLink")
    return link


def parse_end_device_list(xml: bytes | str) -> list[EndDevice]:
    root = _root(xml, "EndDeviceList")
    devices: list[EndDevice] = []
    for el in root.findall(f"{_P}EndDevice"):
        sfdi = _text(el, "sFDI")
        devices.append(
            EndDevice(
                href=str(el.get("href", "")),
                lfdi=(_text(el, "lFDI") or "").upper() or None,
                sfdi=int(sfdi) if sfdi is not None else None,
                fsa_list=_link(el, "FunctionSetAssignmentsListLink"),
            )
        )
    return devices


def parse_fsa_list(xml: bytes | str) -> list[Link]:
    """DERProgramListLinks of every FunctionSetAssignments entry."""
    root = _root(xml, "FunctionSetAssignmentsList")
    links: list[Link] = []
    for fsa in root.findall(f"{_P}FunctionSetAssignments"):
        link = _link(fsa, "DERProgramListLink")
        if link is not None:
            links.append(link)
    return links


def parse_der_program_list(xml: bytes | str) -> list[DerProgram]:
    """Programs sorted by primacy (lower value = higher priority, 2030.5 S10.1)."""
    root = _root(xml, "DERProgramList")
    programs = [
        DerProgram(
            href=str(el.get("href", "")),
            mrid=_text(el, "mRID") or "",
            primacy=int(_text(el, "primacy") or "255"),
            description=_text(el, "description") or "",
            control_list=_link(el, "DERControlListLink"),
            default_control=_link(el, "DefaultDERControlLink"),
        )
        for el in root.findall(f"{_P}DERProgram")
    ]
    return sorted(programs, key=lambda p: p.primacy)


def _control_base(el: Element | None) -> DerControlBase:
    if el is None:
        return DerControlBase()
    target_w: float | None = None
    target = el.find(f"{_P}opModTargetW")
    if target is not None:
        value = _text(target, "value")
        multiplier = _text(target, "multiplier") or "0"
        if value is not None:
            target_w = float(value) * (10 ** int(multiplier))
    max_lim = _text(el, "opModMaxLimW")
    fixed = _text(el, "opModFixedW")
    return DerControlBase(
        op_mod_connect=_bool(_text(el, "opModConnect")),
        op_mod_energize=_bool(_text(el, "opModEnergize")),
        op_mod_max_lim_w_pct=int(max_lim) / 100.0 if max_lim is not None else None,
        op_mod_fixed_w_pct=int(fixed) / 100.0 if fixed is not None else None,
        op_mod_target_w=target_w,
    )


def parse_der_control_list(xml: bytes | str) -> list[DerControl]:
    root = _root(xml, "DERControlList")
    controls: list[DerControl] = []
    for el in root.findall(f"{_P}DERControl"):
        interval = el.find(f"{_P}interval")
        status = el.find(f"{_P}EventStatus")
        start = _ts(_text(interval, "start")) if interval is not None else None
        duration = _text(interval, "duration") if interval is not None else None
        if start is None or duration is None:
            raise Sep2ParseError("DERControl without interval start/duration")
        controls.append(
            DerControl(
                href=el.get("href"),
                mrid=(_text(el, "mRID") or "").upper(),
                description=_text(el, "description") or "",
                creation_time=_ts(_text(el, "creationTime")),
                current_status=int(_text(status, "currentStatus") or "0") if status is not None else 0,
                start=start,
                duration_s=int(duration),
                base=_control_base(el.find(f"{_P}DERControlBase")),
                reply_to=el.get("replyTo"),
                response_required=int(el.get("responseRequired", "0"), 16),
            )
        )
    return controls


def parse_default_der_control(xml: bytes | str) -> DerControlBase:
    root = _root(xml, "DefaultDERControl")
    return _control_base(root.find(f"{_P}DERControlBase"))


def build_der_control_response(*, lfdi: str, mrid: str, status: int, created: datetime) -> bytes:
    """A DERControlResponse (2030.5 S10.10.4.2) for POSTing to a control's `replyTo`."""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<DERControlResponse xmlns="{NS}">'
        f"<createdDateTime>{int(created.timestamp())}</createdDateTime>"
        f"<endDeviceLFDI>{escape(lfdi)}</endDeviceLFDI>"
        f"<status>{status}</status>"
        f"<subject>{escape(mrid)}</subject>"
        f"</DERControlResponse>"
    ).encode()
