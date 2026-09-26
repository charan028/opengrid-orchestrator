"""`opengrid.safestop.cli`: the host-side stop-only CLI (K8). No DB/MQTT: `run_engage` takes the service."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest

import opengrid.safestop.cli as cli
from opengrid.core.crypto import generate_keypair
from opengrid.safestop.keys import StopSigningKey
from opengrid.safestop.service import SafestopService


@dataclass
class _RecordingEngager:
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def engage(self, scope, scope_ref, reason, initiator_ref, *, initiator_kind="SAFESTOP_AUTHORITY"):
        self.calls.append(
            {
                "scope": scope,
                "scope_ref": scope_ref,
                "reason": reason,
                "initiator_ref": initiator_ref,
                "initiator_kind": initiator_kind,
            }
        )
        return uuid4()


def _args(*extra: str) -> list[str]:
    return ["engage", *extra]


def test_valid_bank_request_builds_host_cli_initiator():
    req = cli.validate(
        cli.parse_args(
            _args(
                "--scope", "BANK", "--ref", "bank-007", "--reason", "smoke in cabinet",
                "--operator", "alice", "--confirm", "BANK:bank-007",
            )
        )
    )  # fmt: skip
    assert req == cli.EngageRequest("BANK", "bank-007", "smoke in cabinet", "operator:alice@host-cli")


def test_confirm_mismatch_refuses_and_never_engages(monkeypatch):
    engaged: list[object] = []
    monkeypatch.setattr(cli, "_engage_live", lambda request: engaged.append(request))
    rc = cli.main(
        _args(
            "--scope", "BANK", "--ref", "bank-007", "--reason", "x",
            "--operator", "alice", "--confirm", "BANK:bank-008",
        )
    )  # fmt: skip
    assert rc == cli.EXIT_REFUSED
    assert engaged == []


@pytest.mark.parametrize("confirm", ["bank:bank-007", "BANK:bank-007 ", "ZONE:bank-007", "BANK"])
def test_confirm_must_match_exactly(confirm):
    args = cli.parse_args(
        _args(
            "--scope", "BANK", "--ref", "bank-007", "--reason", "x", "--operator", "a", "--confirm", confirm
        )
    )
    with pytest.raises(cli.CliValidationError):
        cli.validate(args)


def test_fleet_ref_defaults_to_fleet():
    req = cli.validate(
        cli.parse_args(
            _args("--scope", "FLEET", "--reason", "drill", "--operator", "bob", "--confirm", "FLEET:FLEET")
        )
    )
    assert req.scope == "FLEET"
    assert req.scope_ref == "FLEET"


def test_fleet_rejects_other_ref():
    args = cli.parse_args(
        _args(
            "--scope",
            "FLEET",
            "--ref",
            "LZ_NORTH",
            "--reason",
            "d",
            "--operator",
            "b",
            "--confirm",
            "FLEET:LZ_NORTH",
        )
    )
    with pytest.raises(cli.CliValidationError):
        cli.validate(args)


@pytest.mark.parametrize("ref", ["", "bank/007", "bank+", "#", "bank 7"])
def test_bank_ref_must_be_a_single_safe_topic_level(ref):
    args = cli.parse_args(
        _args("--scope", "BANK", "--ref", ref, "--reason", "d", "--operator", "b", "--confirm", f"BANK:{ref}")
    )
    with pytest.raises(cli.CliValidationError):
        cli.validate(args)


def test_release_subcommand_is_rejected():
    with pytest.raises(SystemExit) as exc:
        cli.parse_args(["release", "--scope", "BANK", "--ref", "bank-007"])
    assert exc.value.code != 0


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["engage"],
        ["engage", "--scope", "BANK", "--ref", "b1", "--operator", "a", "--confirm", "BANK:b1"],  # no reason
        ["engage", "--scope", "BANK", "--ref", "b1", "--reason", "r", "--confirm", "BANK:b1"],  # no operator
        ["engage", "--scope", "BANK", "--ref", "b1", "--reason", "r", "--operator", "a"],  # no confirm
        ["engage", "--scope", "PLANET", "--ref", "b1", "--reason", "r", "--operator", "a", "--confirm", "x"],
    ],
)
def test_missing_or_bad_args_rejected(argv):
    with pytest.raises(SystemExit) as exc:
        cli.parse_args(argv)
    assert exc.value.code != 0


async def test_run_engage_calls_service_with_scope_ref_reason_initiator():
    engager = _RecordingEngager()
    req = cli.EngageRequest("ZONE", "LZ_NORTH", "feeder fault", "operator:carol@host-cli")

    result = await cli.run_engage(req, engager)

    assert engager.calls == [
        {
            "scope": "ZONE",
            "scope_ref": "LZ_NORTH",
            "reason": "feeder fault",
            "initiator_ref": "operator:carol@host-cli",
            "initiator_kind": "OPERATOR",
        }
    ]
    assert isinstance(result.stop_id, UUID)
    assert result.topic_suffix == f"stop/zone/LZ_NORTH/{result.stop_id}"


@dataclass
class _OrderedBackend:
    log: list[str]
    rows: list[dict[str, Any]] = field(default_factory=list)

    async def insert_stop_event(self, **kwargs: Any) -> None:
        self.log.append("stop_event")
        self.rows.append(kwargs)


@dataclass
class _OrderedPublisher:
    log: list[str]
    published: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def publish_retained(self, topic_suffix: str, payload: dict[str, Any]) -> None:
        self.log.append("publish")
        self.published.append((topic_suffix, payload))


@dataclass
class _OrderedTrace:
    log: list[str]
    payloads: list[dict[str, Any]] = field(default_factory=list)

    async def append(self, stream_id, decision_type, event_class, payload, reason_codes=None):
        self.log.append("trace")
        self.payloads.append(payload)


async def test_real_service_traces_before_publishing_and_records_operator():
    log: list[str] = []
    seed, _pub = generate_keypair()
    backend = _OrderedBackend(log)
    publisher = _OrderedPublisher(log)
    trace = _OrderedTrace(log)
    service = SafestopService(StopSigningKey("safestop-test", seed), backend, publisher, trace)  # type: ignore[arg-type]
    req = cli.EngageRequest("FLEET", "FLEET", "drill", "operator:dave@host-cli")

    result = await cli.run_engage(req, service)

    assert log == ["trace", "stop_event", "publish"]
    assert log.index("trace") < log.index("publish")
    assert backend.rows[0]["initiator_kind"] == "OPERATOR"
    assert backend.rows[0]["initiator_ref"] == "operator:dave@host-cli"
    assert backend.rows[0]["scope_ref"] == "FLEET"
    topic_suffix, payload = publisher.published[0]
    assert topic_suffix == result.topic_suffix == f"stop/fleet/{result.stop_id}"
    assert payload["action"] == "ENGAGE"
    assert payload["scope_id"] is None
    assert trace.payloads[0]["stop_id"] == str(result.stop_id)


def test_main_reports_expected_failure_without_traceback(monkeypatch, capsys):
    from opengrid.safestop.keys import SafestopKeyError

    async def _boom(request):
        raise SafestopKeyError("cannot read safestop key file /nope")

    monkeypatch.setattr(cli, "_engage_live", _boom)
    rc = cli.main(
        _args("--scope", "BANK", "--ref", "b1", "--reason", "r", "--operator", "a", "--confirm", "BANK:b1")
    )
    assert rc == cli.EXIT_FAILED
    err = capsys.readouterr().err
    assert "SafestopKeyError" in err
    assert "Traceback" not in err
