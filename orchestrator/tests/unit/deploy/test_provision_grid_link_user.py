"""deploy/mosquitto/provision_grid_link_user.py: the og_gridlink MQTT user (D-34). Pure planning only; nothing
touches a real broker or password file."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parents[4] / "deploy" / "mosquitto"


def _load(name: str) -> object:
    spec = importlib.util.spec_from_file_location(name, _DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pws = _load("provision_ws_users")
pgl = _load("provision_grid_link_user")

ACL = (
    "user og_engine\ntopic read og/v1/tel/#\n\nuser og_guardian\ntopic read og/v1/scada/instruction/#\n\n"
    f"{pws.BEGIN_MARKER}\nuser ogw_sims\ntopic readwrite ogtest/sims/#\n\n{pws.END_MARKER}\n"  # type: ignore[attr-defined]
)
PASSWORD = "x" * 48


def test_acl_block_is_write_only_on_instructions_and_before_the_workspace_block() -> None:
    new = pgl.render_acl(ACL, "og/v1")  # type: ignore[attr-defined]
    lines = new.splitlines()
    at = lines.index("user og_gridlink")
    assert lines[at + 1] == "topic write og/v1/scada/instruction/#"
    assert at < lines.index(pws.BEGIN_MARKER)  # type: ignore[attr-defined]
    assert new.count("user og_gridlink") == 1
    assert pgl.render_acl(new, "og/v1") == new  # type: ignore[attr-defined]  # idempotent
    assert pgl.other_grants_unchanged(ACL, new)  # type: ignore[attr-defined]


def test_acl_without_workspace_block_appends_and_other_grants_stay() -> None:
    plain = "user og_engine\ntopic read og/v1/tel/#\n"
    new = pgl.render_acl(plain, "og/v1")  # type: ignore[attr-defined]
    assert new.startswith(plain) and new.rstrip().endswith(pgl.END)  # type: ignore[attr-defined]
    assert not pgl.other_grants_unchanged(plain, new.replace("og/v1/tel/#", "og/v1/#"))  # type: ignore[attr-defined]


def test_password_hash_set_once_then_kept() -> None:
    current = "og_engine:$7$101$abc$def\n"
    first, changed = pgl.plan_passwd(current, PASSWORD)  # type: ignore[attr-defined]
    assert changed and first.startswith(current) and "og_gridlink:$7$" in first and PASSWORD not in first
    again, changed_again = pgl.plan_passwd(first, PASSWORD)  # type: ignore[attr-defined]
    assert again == first and not changed_again
