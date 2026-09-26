"""deploy/mosquitto/provision_ws_users.py: per-workspace MQTT users. Pure planning is tested here against
the production ACL as it stood on 2026-09-26 (plus the historical shared ogtest pattern line); nothing
touches a real broker, password file or /opt/opengrid."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[4] / "deploy" / "mosquitto" / "provision_ws_users.py"
_spec = importlib.util.spec_from_file_location("provision_ws_users", _SCRIPT)
assert _spec is not None and _spec.loader is not None
pws = importlib.util.module_from_spec(_spec)
sys.modules["provision_ws_users"] = pws
_spec.loader.exec_module(pws)

#: Production /etc/mosquitto/opengrid.acl, read on 2026-09-26, with the shared pattern line it carried
#: until 2026-09-25 20:49 restored at line 2 so the removal is exercised.
PRODUCTION_ACL = """# production topic root og/v1 ; test root ogtest/<workspace>/... (all og users)
pattern readwrite ogtest/#

user og_sim
topic write og/v1/tel/#
topic write og/v1/ack/#
topic read og/v1/cmd/#
topic read og/v1/stop/#
topic read og/v1/lease/#
topic write og/v1/scada/#
topic read og/v1/scada/ctl/#
topic readwrite og/v1/scenario/#

user og_simctl
topic readwrite og/v1/scenario/#
topic write og/v1/scada/#
topic read og/v1/#

user og_engine
topic read og/v1/tel/#
topic read og/v1/ack/#
topic read og/v1/scada/#
topic read og/v1/stop/#

user og_guardian
topic write og/v1/cmd/#
topic write og/v1/lease/#
topic read og/v1/ack/#
topic read og/v1/tel/#
topic read og/v1/scada/instruction/#

user og_safestop
topic write og/v1/stop/#
topic read og/v1/ack/#

user og_api
topic read og/v1/#
"""
PRODUCTION_USERS = ["og_engine", "og_guardian", "og_sim", "og_api", "og_safestop", "og_simctl"]
#: mosquitto_passwd 2.0.21 output for a throwaway user (not a real credential), recorded on the server.
MOSQUITTO_DUMMY = (
    "dummyuser",
    "dummy-not-a-secret",
    "$7$101$nUZePF5feTCvMZ4h$2tudxWtF46Z1XXY6XWYX1hWwII/nt2Hxsbv/JWKCZUhXwRvvuY+BN4qJkHbr12BQY3kG5/Y0lUSRHQiPT3YrIQ==",
)


def _passwd() -> str:
    return "".join(f"{user}:$7$101$c2FsdHNhbHRzYWx0$aGFzaA==\n" for user in PRODUCTION_USERS)


def _counter():
    count = {"n": 0}

    def make() -> str:
        count["n"] += 1
        return f"generated-password-{count['n']}"

    return make


def test_hashing_matches_mosquitto_passwd():
    _user, password, stored = MOSQUITTO_DUMMY
    assert pws.password_matches(password, stored)
    assert not pws.password_matches("wrong", stored)
    fresh = pws.hash_password("anything")
    assert fresh.startswith("$7$101$") and pws.password_matches("anything", fresh)


def test_workspaces_are_discovered_from_dirs_and_databases():
    assert pws.discover_workspaces(
        ["guard", "sims", "guard.new", "Bad", "x-y"], ["og_t_alloc", "og_t_guard", "og_test"]
    ) == [
        "alloc",
        "guard",
        "sims",
    ]


def test_acl_removes_the_shared_pattern_adds_one_block_per_workspace_and_keeps_production_identical():
    new_acl = pws.render_acl(PRODUCTION_ACL, ["guard", "sims"])

    assert "pattern readwrite ogtest/#" not in new_acl
    assert "user ogw_guard\ntopic readwrite ogtest/guard/#" in new_acl
    assert "user ogw_sims\ntopic readwrite ogtest/sims/#" in new_acl
    assert pws.production_grants(new_acl) == pws.production_grants(PRODUCTION_ACL)
    assert pws.render_acl(new_acl, ["guard", "sims"]) == new_acl  # idempotent


def test_production_diff_is_only_the_removed_pattern_line():
    """Requirement 4: outside the managed workspace block, the only change is the removed ogtest pattern."""
    new_acl = pws.render_acl(PRODUCTION_ACL, ["guard"])
    before = PRODUCTION_ACL.splitlines()
    outside = pws.strip_managed(new_acl).splitlines()
    removed = [line for line in before if line not in outside]
    assert removed == ["pattern readwrite ogtest/#"]
    assert [line for line in outside if line not in before] == []


def test_plan_adds_users_and_env_files_then_is_idempotent(tmp_path):
    plan = pws.build_plan(["guard", "sims"], PRODUCTION_ACL, _passwd(), tmp_path, new_password=_counter())

    assert plan.user_actions == {"ogw_guard": "add", "ogw_sims": "add"}
    assert set(plan.env_files) == {tmp_path / "guard" / ".mqtt.env", tmp_path / "sims" / ".mqtt.env"}
    assert [user for user, _ in plan.new_passwd][: len(PRODUCTION_USERS)] == PRODUCTION_USERS
    assert dict(plan.new_passwd)["og_guardian"] == dict(pws.parse_passwd(_passwd()))["og_guardian"]

    for path, text in plan.env_files.items():  # what --apply writes
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    again = pws.build_plan(["guard", "sims"], plan.new_acl, pws.render_passwd(plan.new_passwd), tmp_path)
    assert again.user_actions == {"ogw_guard": "unchanged", "ogw_sims": "unchanged"}
    assert again.env_files == {} and again.new_acl == plan.new_acl and again.new_passwd == plan.new_passwd


def test_existing_env_password_is_rehashed_not_rotated_when_the_passwd_entry_is_missing(tmp_path):
    env = tmp_path / "guard" / ".mqtt.env"
    env.parent.mkdir()
    env.write_text(pws.env_file_text("ogw_guard", "kept-password"), encoding="utf-8")

    plan = pws.build_plan(["guard"], PRODUCTION_ACL, _passwd(), tmp_path)

    assert plan.user_actions == {"ogw_guard": "rehash"} and plan.env_files == {}
    assert pws.password_matches("kept-password", dict(plan.new_passwd)["ogw_guard"])


def test_missing_env_file_rotates_an_existing_user(tmp_path):
    passwd = _passwd() + "ogw_guard:" + pws.hash_password("lost") + "\n"
    plan = pws.build_plan(["guard"], PRODUCTION_ACL, passwd, tmp_path, new_password=_counter())
    assert plan.user_actions == {"ogw_guard": "rotate"}
    assert pws.password_matches("generated-password-1", dict(plan.new_passwd)["ogw_guard"])


def test_dry_run_report_contains_no_secrets(tmp_path):
    plan = pws.build_plan(["guard"], PRODUCTION_ACL, _passwd(), tmp_path, new_password=_counter())
    report = pws.describe(plan, PRODUCTION_ACL)

    assert "generated-password" not in report and "$7$" not in report and "OG_MQTT_WS_PASSWORD" not in report
    assert "-pattern readwrite ogtest/#" in report
    assert "+user ogw_guard" in report and "+topic readwrite ogtest/guard/#" in report
    assert "ogw_guard: add" in report and str(tmp_path / "guard" / ".mqtt.env") in report
    assert "production grants: unchanged" in report


def test_env_file_content_is_only_the_workspace_credentials():
    text = pws.env_file_text("ogw_guard", "pw")
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    assert lines == ["OG_MQTT_WS_USER=ogw_guard", "OG_MQTT_WS_PASSWORD=pw"]


def test_validation_refuses_a_change_to_production_grants():
    tampered = pws.render_acl(PRODUCTION_ACL, ["guard"]).replace(
        "topic read og/v1/tel/#", "topic readwrite og/v1/#", 1
    )
    with pytest.raises(pws.ProvisionError, match="production grants"):
        pws.validate(PRODUCTION_ACL, tampered, [("ogw_guard", "x")], ["guard"])


def test_validation_refuses_bad_syntax_and_missing_users():
    good = pws.render_acl(PRODUCTION_ACL, ["guard"])
    with pytest.raises(pws.ProvisionError, match="syntax"):
        pws.validate(PRODUCTION_ACL, good + "topic sometimes og/v1/#\n", [("ogw_guard", "x")], ["guard"])
    with pytest.raises(pws.ProvisionError, match="missing"):
        pws.validate(PRODUCTION_ACL, good, [], ["guard"])


def test_cli_requires_an_explicit_mode():
    with pytest.raises(SystemExit):
        pws.main([])
