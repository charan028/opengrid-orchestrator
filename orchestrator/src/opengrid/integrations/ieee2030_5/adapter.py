"""IEEE 2030.5 DER-control client mapped to utility instructions (protocol-adapters.md S4).

The orchestrator acts as a 2030.5 CLIENT (an aggregator in CSIP terms) of the utility's 2030.5 server.
Each poll walks DeviceCapability -> EndDeviceList -> (EndDevice mapped to a bank by LFDI or SFDI) ->
FunctionSetAssignments -> DERProgramList -> DERControlList / DefaultDERControl, and reduces the
controls of each bank to one `DesiredInstruction`:

- the highest-primacy program (lowest `primacy` value) that has an ACTIVE control wins; with no active
  event, that program's DefaultDERControl applies (a standing limit), if it maps to anything;
- `opModEnergize = false` -> ESTOP; `opModConnect = false` -> BLOCK;
- `opModMaxLimW` (% of setMaxW) / `opModFixedW` (signed %, discharge side) / `opModTargetW` (W) ->
  LIMIT, converted to kW with the bank's configured `rated_kw` (the DER's setMaxW);
- an event's `interval` end becomes the instruction's `expires_at`; a cancelled or finished event lifts
  the instruction (`expires_at == issued_at`), the convention the fleet and guardian already honour.

When a control carries `replyTo` and a non-zero `responseRequired`, the client POSTs DERControlResponse
status 1 (received) when it first sees the event and 2 (started) when it maps it to an instruction.

This backend does not produce bank analogs: 2030.5 is a control channel here. Bank loading keeps
arriving over the configured measurement path (MQTT or DNP3).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from opengrid.integrations.ieee2030_5.resources import (
    SEP_MEDIA_TYPE,
    DerControl,
    DerControlBase,
    EndDevice,
    Sep2ParseError,
    build_der_control_response,
    parse_default_der_control,
    parse_der_control_list,
    parse_der_program_list,
    parse_device_capability,
    parse_end_device_list,
    parse_fsa_list,
)
from opengrid.integrations.instructions import DesiredInstruction, InstructionTracker
from opengrid.integrations.interfaces import ScadaSink
from opengrid.integrations.tls import TlsSettings, build_client_ssl_context

logger = logging.getLogger(__name__)

__all__ = ["Ieee20305BankMapping", "Ieee20305ScadaSource", "Ieee20305Settings", "desired_from_control_base"]

RESPONSE_RECEIVED = 1
RESPONSE_STARTED = 2


class Ieee20305BankMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bank_id: str
    lfdi: str | None = None
    sfdi: int | None = None
    rated_kw: float = Field(gt=0)  # the DER's setMaxW, in kW

    @model_validator(mode="after")
    def _identified(self) -> Ieee20305BankMapping:
        if self.lfdi is None and self.sfdi is None:
            raise ValueError("a 2030.5 bank mapping needs an lfdi or an sfdi")
        return self


class Ieee20305Settings(BaseModel):
    """`[integrations.scada.ieee2030_5]`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str  # e.g. https://utility-2030-5.example:8443
    dcap_path: str = "/dcap"
    poll_s: float = Field(default=10.0, gt=0)
    timeout_s: float = Field(default=10.0, gt=0)
    client_lfdi: str = ""  # our aggregator LFDI (from our certificate), sent in DERControlResponse
    banks: list[Ieee20305BankMapping] = Field(min_length=1)
    tls: TlsSettings = TlsSettings()
    send_responses: bool = True
    issued_by: str = "UTILITY_IEEE2030_5"


def desired_from_control_base(
    base: DerControlBase, *, rated_kw: float, expires_at: datetime | None, key: str
) -> DesiredInstruction | None:
    """Map one DERControlBase to an instruction (module docstring); `None` if nothing maps."""
    if base.op_mod_energize is False:
        return DesiredInstruction(kind="ESTOP", expires_at=expires_at, key=key)
    if base.op_mod_connect is False:
        return DesiredInstruction(kind="BLOCK", expires_at=expires_at, key=key)
    limits: list[float] = []
    if base.op_mod_max_lim_w_pct is not None:
        limits.append(rated_kw * base.op_mod_max_lim_w_pct / 100.0)
    if base.op_mod_fixed_w_pct is not None:
        limits.append(rated_kw * max(0.0, base.op_mod_fixed_w_pct) / 100.0)
    if base.op_mod_target_w is not None:
        limits.append(max(0.0, base.op_mod_target_w / 1000.0))
    if not limits:
        return None
    return DesiredInstruction(kind="LIMIT", limit_kw=round(min(limits), 3), expires_at=expires_at, key=key)


class Ieee20305ScadaSource:
    """`ScadaSource` over IEEE 2030.5 (instructions only)."""

    def __init__(
        self,
        settings: Ieee20305Settings,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))
        self._client = client or httpx.AsyncClient(
            base_url=settings.base_url,
            verify=build_client_ssl_context(settings.tls) or True,
            timeout=settings.timeout_s,
            headers={"Accept": SEP_MEDIA_TYPE},
        )
        self._tracker = InstructionTracker(source="ieee2030_5", issued_by=settings.issued_by)
        self._responded: set[tuple[str, int]] = set()

    @property
    def backend(self) -> str:
        return "ieee2030_5"

    async def _get(self, href: str) -> bytes:
        response = await self._client.get(href, headers={"Accept": SEP_MEDIA_TYPE})
        response.raise_for_status()
        return response.content

    def _mapping_for(self, device: EndDevice) -> Ieee20305BankMapping | None:
        for mapping in self._settings.banks:
            if mapping.lfdi is not None and device.lfdi == mapping.lfdi.upper():
                return mapping
            if mapping.sfdi is not None and device.sfdi == mapping.sfdi:
                return mapping
        return None

    async def _respond(self, control: DerControl, status: int) -> None:
        if not self._settings.send_responses or not control.reply_to or control.response_required == 0:
            return
        if (control.mrid, status) in self._responded:
            return
        body = build_der_control_response(
            lfdi=self._settings.client_lfdi, mrid=control.mrid, status=status, created=self._clock()
        )
        try:
            response = await self._client.post(
                control.reply_to, content=body, headers={"Content-Type": SEP_MEDIA_TYPE}
            )
            response.raise_for_status()
            self._responded.add((control.mrid, status))
        except httpx.HTTPError as exc:
            logger.warning(
                "2030.5 DERControlResponse failed", extra={"mrid": control.mrid, "error": str(exc)}
            )

    async def _desired_for_device(
        self, device: EndDevice, mapping: Ieee20305BankMapping, now: datetime
    ) -> DesiredInstruction | None:
        if device.fsa_list is None:
            return None
        for program_list in parse_fsa_list(await self._get(device.fsa_list.href)):
            for program in parse_der_program_list(await self._get(program_list.href)):
                active: list[DerControl] = []
                if program.control_list is not None:
                    for control in parse_der_control_list(await self._get(program.control_list.href)):
                        await self._respond(control, RESPONSE_RECEIVED)
                        if control.is_active(now):
                            active.append(control)
                for control in sorted(active, key=lambda c: c.creation_time or c.start, reverse=True):
                    desired = desired_from_control_base(
                        control.base, rated_kw=mapping.rated_kw, expires_at=control.end, key=control.mrid
                    )
                    if desired is not None:
                        await self._respond(control, RESPONSE_STARTED)
                        return desired
                if program.default_control is not None:
                    base = parse_default_der_control(await self._get(program.default_control.href))
                    desired = desired_from_control_base(
                        base, rated_kw=mapping.rated_kw, expires_at=None, key=f"default:{program.mrid}"
                    )
                    if desired is not None:
                        return desired
        return None

    async def poll_once(self, sink: ScadaSink) -> int:
        """Walk the resource tree once; deliver instruction changes. Raises on HTTP/parse failure."""
        now = self._clock()
        edev_link = parse_device_capability(await self._get(self._settings.dcap_path))
        desired_by_bank: dict[str, DesiredInstruction | None] = {
            m.bank_id: None for m in self._settings.banks
        }
        for device in parse_end_device_list(await self._get(edev_link.href)):
            mapping = self._mapping_for(device)
            if mapping is not None:
                desired_by_bank[mapping.bank_id] = await self._desired_for_device(device, mapping, now)
        delivered = 0
        for bank_id, desired in desired_by_bank.items():
            instruction = self._tracker.update(bank_id, desired, now=now)
            if instruction is not None:
                await sink.on_utility_instruction(instruction)
                delivered += 1
        return delivered

    async def run(self, sink: ScadaSink) -> None:
        while True:
            try:
                await self.poll_once(sink)
            except (httpx.HTTPError, Sep2ParseError) as exc:
                # Keep the last delivered instructions in force (they carry their own expiry); never
                # lift a utility limit because the utility's server was unreachable.
                logger.warning("2030.5 poll failed", extra={"error": str(exc)})
            except Exception:
                logger.exception("2030.5 poll raised")
            await asyncio.sleep(self._settings.poll_s)

    async def close(self) -> None:
        await self._client.aclose()
