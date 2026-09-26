"""Pure policy engine: rules as data (role x action x resource-scope), deny by default.

No I/O here (BUILD.md S5a "pure logic separated from I/O", the same split `opengrid.trace.store` /
`opengrid.trace.pg_backend` use) -- `load_policies` takes an already-parsed dict (from `tomllib.load`,
never re-implemented here) and `decide()` is a plain function over in-memory `Rule` objects. Callers own
reading the file and any I/O that follows a decision (e.g. `opengrid.authz.enforce`'s trace-store audit
write on a privileged deny).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Rule:
    role: str
    action: str
    resource_scope: str = "global"  # "global" | "own"
    audited: bool = False

    def matches_action(self, action: str) -> bool:
        if self.action == action:
            return True
        if self.action.endswith(".*"):
            return action.startswith(self.action[:-1])  # strip the trailing "*", keep the dot
        return False


@dataclass(frozen=True, slots=True)
class Resource:
    """The thing an action acts on, for `resource_scope = "own"` checks. `customer_id is None` means
    the resource carries no customer scope at all (e.g. a fleet-wide read) -- an "own"-scoped rule then
    refuses it rather than treating a missing id as a match."""

    customer_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    allow: bool
    reason: str
    audited: bool = False

    @property
    def deny(self) -> bool:
        return not self.allow


def _allow(rule: Rule) -> Decision:
    return Decision(True, f"allowed by rule role={rule.role!r} action={rule.action!r}", rule.audited)


def _deny(reason: str, *, audited: bool = False) -> Decision:
    return Decision(False, reason, audited)


class PolicyEngine:
    """A loaded, immutable rule set. `decide()` is deny-by-default: an action with no matching rule for
    the caller's role is refused, and the refusal is marked `audited=True` whenever ANY rule for that
    action (any role) asks to be audited -- so a privileged action stays audited even when the denied
    caller's role has no rule for it at all."""

    def __init__(self, rules: list[Rule]) -> None:
        self._rules = rules

    @property
    def rules(self) -> list[Rule]:
        return list(self._rules)

    def _audited_for_action(self, action: str) -> bool:
        return any(r.audited for r in self._rules if r.matches_action(action))

    def decide(
        self,
        *,
        role: str,
        action: str,
        resource: Resource | None = None,
        caller_customer_id: UUID | None = None,
    ) -> Decision:
        """Deny by default. `caller_customer_id` is the identity's own customer_id (customer role
        only); `resource` describes what is being acted on. A "own"-scoped rule allows only when both
        ids are present and equal -- a customer can never reach another customer's resource, and never
        a resource with no declared owner."""
        matching = [r for r in self._rules if r.role == role and r.matches_action(action)]
        if not matching:
            return _deny(
                f"no rule allows role={role!r} action={action!r}", audited=self._audited_for_action(action)
            )

        for rule in matching:
            if rule.resource_scope == "global":
                return _allow(rule)
            if rule.resource_scope == "own":
                resource_customer_id = resource.customer_id if resource is not None else None
                if (
                    caller_customer_id is not None
                    and resource_customer_id is not None
                    and caller_customer_id == resource_customer_id
                ):
                    return _allow(rule)

        return _deny(
            f"role={role!r} action={action!r} matched only own-scoped rules and the resource is out of scope",
            audited=self._audited_for_action(action),
        )


def _rule_from_dict(entry: dict[str, Any]) -> Rule:
    role = str(entry["role"])
    action = str(entry["action"])
    resource_scope = str(entry.get("resource_scope", "global"))
    audited = bool(entry.get("audited", False))
    return Rule(role=role, action=action, resource_scope=resource_scope, audited=audited)


def load_policies(data: dict[str, Any]) -> PolicyEngine:
    """Build a `PolicyEngine` from an already-parsed TOML dict (top-level `[[rule]]` array of tables)."""
    entries = data.get("rule", [])
    if not isinstance(entries, list):
        raise ValueError("authz policy: top-level 'rule' must be an array of tables ([[rule]])")
    return PolicyEngine([_rule_from_dict(e) for e in entries])


def load_policies_from_file(path: str) -> PolicyEngine:
    """Convenience for real callers: read+parse `path` (a TOML file, e.g. `config/authz.toml`)."""
    import tomllib
    from pathlib import Path

    with Path(path).open("rb") as fh:
        data = tomllib.load(fh)
    return load_policies(data)
