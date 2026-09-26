"""IEEE 2030.5 DER-control client against the ogsim 2030.5 server, fully in-process (ASGI transport)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from opengrid.integrations.ieee2030_5.adapter import (
    Ieee20305BankMapping,
    Ieee20305ScadaSource,
    Ieee20305Settings,
    desired_from_control_base,
)
from opengrid.integrations.ieee2030_5.identity import lfdi_from_certificate, sfdi_from_lfdi
from opengrid.integrations.ieee2030_5.resources import (
    DerControlBase,
    Sep2ParseError,
    parse_der_control_list,
    parse_device_capability,
)
from opengrid.integrations.sinks import CollectingSink

sim = pytest.importorskip("ogsim.protocols.ieee2030_5_server")

LFDI_A = "3E4F45AB31EDFE5B67E343E5E4562E31984E23E5"
LFDI_B = "0123456789ABCDEF0123456789ABCDEF01234567"
T0 = 1_790_000_000.0


class _Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def unix(self) -> float:
        return self.t

    def dt(self) -> datetime:
        return datetime.fromtimestamp(self.t, tz=UTC)


@pytest.fixture
def world() -> tuple[_Clock, object, Ieee20305ScadaSource, httpx.AsyncClient]:
    clock = _Clock(T0)
    state = sim.Sep2ServerState(clock=clock.unix)
    state.add_device(LFDI_A, sfdi=sfdi_from_lfdi(LFDI_A))
    state.add_device(LFDI_B)
    app = sim.create_app(state)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sep2.test")
    settings = Ieee20305Settings(
        base_url="http://sep2.test",
        client_lfdi="AA" * 20,
        banks=[
            Ieee20305BankMapping(bank_id="bank-000", sfdi=sfdi_from_lfdi(LFDI_A), rated_kw=600.0),
            Ieee20305BankMapping(bank_id="bank-001", lfdi=LFDI_B.lower(), rated_kw=400.0),
        ],
    )
    source = Ieee20305ScadaSource(settings, client=client, clock=clock.dt)
    return clock, state, source, client


async def test_no_controls_delivers_nothing(
    world: tuple[_Clock, object, Ieee20305ScadaSource, object],
) -> None:
    _, _, source, _ = world
    sink = CollectingSink()
    assert await source.poll_once(sink) == 0
    assert sink.instructions == []


async def test_max_limit_control_maps_to_limit_then_lifts_on_cancel(
    world: tuple[_Clock, object, Ieee20305ScadaSource, httpx.AsyncClient],
) -> None:
    clock, state, source, client = world
    resp = await client.post("/admin/devices/0/controls", json={"duration": 1800, "opModMaxLimW": 5000})
    mrid = resp.json()["mrid"]
    sink = CollectingSink()
    assert await source.poll_once(sink) == 1
    instr = sink.instructions[0]
    assert (instr.bank_id, instr.kind, instr.limit_kw) == ("bank-000", "LIMIT", 300.0)
    assert instr.expires_at == clock.dt() + timedelta(seconds=1800)
    # unchanged on the next poll: no duplicate instruction
    assert await source.poll_once(sink) == 0
    # the client acknowledged: received (1) then started (2), with our LFDI
    statuses = sorted(int(r["status"]) for r in state.responses)  # type: ignore[attr-defined]
    assert statuses == [1, 2]
    assert all(r["subject"] == mrid for r in state.responses)  # type: ignore[attr-defined]
    await client.post(f"/admin/controls/{mrid}/cancel")
    clock.t += 5
    assert await source.poll_once(sink) == 1
    lifted = sink.instructions[-1]
    assert lifted.kind == "LIMIT" and lifted.expires_at == lifted.issued_at


async def test_energize_false_is_estop_and_wins_over_limit(
    world: tuple[_Clock, object, Ieee20305ScadaSource, httpx.AsyncClient],
) -> None:
    clock, _, source, client = world
    await client.post("/admin/devices/1/controls", json={"duration": 600, "opModMaxLimW": 2500})
    clock.t += 1
    await client.post("/admin/devices/1/controls", json={"duration": 600, "opModEnergize": False})
    sink = CollectingSink()
    await source.poll_once(sink)
    assert [(i.bank_id, i.kind) for i in sink.instructions] == [("bank-001", "ESTOP")]


async def test_scheduled_future_event_is_not_active_until_start(
    world: tuple[_Clock, object, Ieee20305ScadaSource, httpx.AsyncClient],
) -> None:
    clock, _, source, client = world
    await client.post(
        "/admin/devices/0/controls", json={"start": int(T0) + 60, "duration": 60, "opModConnect": False}
    )
    sink = CollectingSink()
    assert await source.poll_once(sink) == 0
    clock.t += 61
    assert await source.poll_once(sink) == 1
    assert sink.instructions[-1].kind == "BLOCK"
    clock.t += 60  # interval over -> lifted
    assert await source.poll_once(sink) == 1
    assert sink.instructions[-1].expires_at == sink.instructions[-1].issued_at


async def test_default_der_control_is_a_standing_limit(
    world: tuple[_Clock, object, Ieee20305ScadaSource, httpx.AsyncClient],
) -> None:
    _, _, source, client = world
    await client.put("/admin/devices/1/default", json={"opModTargetW": 150000})
    sink = CollectingSink()
    await source.poll_once(sink)
    instr = sink.instructions[0]
    assert (instr.bank_id, instr.kind, instr.limit_kw, instr.expires_at) == ("bank-001", "LIMIT", 150.0, None)


def test_control_base_mapping_takes_the_most_restrictive_limit() -> None:
    base = DerControlBase(op_mod_max_lim_w_pct=50.0, op_mod_fixed_w_pct=-10.0, op_mod_target_w=250_000.0)
    desired = desired_from_control_base(base, rated_kw=600.0, expires_at=None, key="k")
    assert desired is not None and desired.kind == "LIMIT" and desired.limit_kw == 0.0
    assert desired_from_control_base(DerControlBase(), rated_kw=600.0, expires_at=None, key="k") is None


def test_parsers_reject_wrong_namespace_and_entities() -> None:
    with pytest.raises(Sep2ParseError):
        parse_device_capability(b'<DeviceCapability xmlns="urn:other"/>')
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]>'
        b'<DERControlList xmlns="urn:ieee:std:2030.5:ns">&a;</DERControlList>'
    )
    with pytest.raises(Sep2ParseError):
        parse_der_control_list(bomb)


def test_lfdi_and_sfdi_from_certificate() -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "og-aggregator")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    lfdi = lfdi_from_certificate(cert.public_bytes(serialization.Encoding.PEM))
    assert len(lfdi) == 40 and lfdi == lfdi.upper()
    sfdi = sfdi_from_lfdi(lfdi)
    assert sum(int(d) for d in str(sfdi)) % 10 == 0
    assert sfdi // 10 == int(lfdi[:9], 16)
