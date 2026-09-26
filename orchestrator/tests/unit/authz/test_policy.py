"""Pure `PolicyEngine.decide` tests: deny-by-default, the roles/actions `config/authz.toml` declares,
and customer own-scope enforcement (cross-customer access denied even with a matching role+action)."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from opengrid.authz.policy import PolicyEngine, Resource, Rule, load_policies, load_policies_from_file

REPO_AUTHZ_TOML = Path(__file__).resolve().parents[3] / "config" / "authz.toml"

CUSTOMER_A = UUID("00000000-0000-7000-8000-0000000000a1")
CUSTOMER_B = UUID("00000000-0000-7000-8000-0000000000b2")


def test_deny_by_default_for_unknown_role_action_pair() -> None:
    engine = PolicyEngine([Rule(role="viewer", action="api.read")])
    decision = engine.decide(role="customer", action="api.read")
    assert decision.deny
    assert "no rule allows" in decision.reason


def test_allow_matches_exact_action() -> None:
    engine = PolicyEngine([Rule(role="operator", action="api.write")])
    decision = engine.decide(role="operator", action="api.write")
    assert decision.allow


def test_wildcard_action_matches_prefix() -> None:
    engine = PolicyEngine([Rule(role="admin", action="admin.*")])
    assert engine.decide(role="admin", action="admin.seed_fleet_topology").allow
    assert engine.decide(role="admin", action="administrivia").deny  # no dot after the prefix -> no match


def test_own_scope_allows_matching_customer_id() -> None:
    engine = PolicyEngine([Rule(role="customer", action="customer.read", resource_scope="own")])
    decision = engine.decide(
        role="customer",
        action="customer.read",
        resource=Resource(customer_id=CUSTOMER_A),
        caller_customer_id=CUSTOMER_A,
    )
    assert decision.allow


def test_own_scope_denies_cross_customer_access() -> None:
    engine = PolicyEngine([Rule(role="customer", action="customer.read", resource_scope="own")])
    decision = engine.decide(
        role="customer",
        action="customer.read",
        resource=Resource(customer_id=CUSTOMER_B),
        caller_customer_id=CUSTOMER_A,
    )
    assert decision.deny


def test_own_scope_denies_when_resource_has_no_declared_owner() -> None:
    """A fleet-wide resource (no customer_id at all) must never satisfy an "own" rule -- a missing id is
    not a wildcard match."""
    engine = PolicyEngine([Rule(role="customer", action="customer.read", resource_scope="own")])
    decision = engine.decide(
        role="customer",
        action="customer.read",
        resource=Resource(customer_id=None),
        caller_customer_id=CUSTOMER_A,
    )
    assert decision.deny


def test_deny_of_audited_action_is_marked_audited_even_for_a_role_with_no_rule() -> None:
    engine = PolicyEngine([Rule(role="operator", action="safestop.engage", audited=True)])
    decision = engine.decide(role="customer", action="safestop.engage")
    assert decision.deny
    assert decision.audited


def test_deny_of_non_audited_action_is_not_audited() -> None:
    engine = PolicyEngine([Rule(role="operator", action="api.read")])
    decision = engine.decide(role="customer", action="api.read")
    assert decision.deny
    assert not decision.audited


def test_load_policies_from_dict() -> None:
    engine = load_policies({"rule": [{"role": "viewer", "action": "api.read"}]})
    assert engine.decide(role="viewer", action="api.read").allow
    assert engine.decide(role="operator", action="api.read").deny


# --- the shipped config/authz.toml: every existing route's current allow/deny is preserved -----------


def test_shipped_policy_file_loads() -> None:
    engine = load_policies_from_file(str(REPO_AUTHZ_TOML))
    assert engine.rules  # non-empty


def test_shipped_policy_preserves_viewer_and_operator_reads() -> None:
    engine = load_policies_from_file(str(REPO_AUTHZ_TOML))
    assert engine.decide(role="viewer", action="api.read").allow
    assert engine.decide(role="operator", action="api.read").allow
    assert engine.decide(role="customer", action="api.read").deny


def test_shipped_policy_preserves_operator_only_writes() -> None:
    engine = load_policies_from_file(str(REPO_AUTHZ_TOML))
    assert engine.decide(role="operator", action="api.write").allow
    assert engine.decide(role="viewer", action="api.write").deny
    assert engine.decide(role="customer", action="api.write").deny


def test_shipped_policy_customer_own_scope_actions_deny_cross_customer() -> None:
    engine = load_policies_from_file(str(REPO_AUTHZ_TOML))
    for action in ("customer.read", "customer.cancel", "customer.renominate"):
        allowed = engine.decide(
            role="customer",
            action=action,
            resource=Resource(customer_id=CUSTOMER_A),
            caller_customer_id=CUSTOMER_A,
        )
        denied = engine.decide(
            role="customer",
            action=action,
            resource=Resource(customer_id=CUSTOMER_B),
            caller_customer_id=CUSTOMER_A,
        )
        assert allowed.allow, action
        assert denied.deny, action


def test_shipped_policy_privileged_operator_actions_are_audited_on_deny() -> None:
    engine = load_policies_from_file(str(REPO_AUTHZ_TOML))
    for action in (
        "safestop.engage",
        "safestop.release.approve",
        "dispatch.as_deploy",
        "contracts.write",
        "fleet.command.bulk",
    ):
        decision = engine.decide(role="viewer", action=action)
        assert decision.deny, action
        assert decision.audited, action
