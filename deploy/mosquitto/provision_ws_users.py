#!/usr/bin/env python3
"""Per-workspace MQTT users for the shared broker (owner-approved 2026-09-26).

Every agent workspace gets its own broker user `ogw_<ws>` that can reach ONLY `ogtest/<ws>/#`, and the
shared `pattern readwrite ogtest/#` grant is removed, so no workspace can publish into production (`og/v1`)
or into another workspace, and workspace runs never need production credentials.

    provision_ws_users.py --dry-run            # print the plan and the ACL diff; changes nothing
    provision_ws_users.py --apply              # back up, apply, check, `systemctl reload mosquitto`
    provision_ws_users.py --apply --workspaces "guard sims"   # only these workspaces

What --apply does, in order (idempotent: re-running changes nothing that is already right):

1. Workspaces = directories under /opt/opengrid/work/ plus og_t_<ws> databases (names ^[a-z0-9_]+$).
2. For each workspace: keep the password already in /opt/opengrid/work/<ws>/.mqtt.env when the password
   file's `ogw_<ws>` entry matches it; otherwise reuse that password (re-hash) or, if there is none, make a
   new random one. The password is written only to that .mqtt.env (mode 0600, owner opengrid) and as a
   PBKDF2-SHA512 hash (mosquitto 2.x `$7$` format) to the password file. It is never printed or logged,
   and never placed on a command line.
3. The ACL gets one managed block (between the BEGIN/END markers below) with `user ogw_<ws>` +
   `topic readwrite ogtest/<ws>/#` per workspace, and every `pattern readwrite ogtest/#` line is removed.
   Nothing else in the ACL changes: the script refuses to apply if the production users' grants would.
4. Every file it edits is first backed up next to itself as `<file>.bak.<UTC timestamp>` (same owner and
   mode). It then checks the result (ACL syntax, every ogw_ user in both files, production grants
   byte-identical), writes atomically, and runs `systemctl reload mosquitto` (SIGHUP; never a restart).
   If mosquitto is not active afterwards, it restores the backups and reloads again.

Rollback (manual): copy the `.bak.<timestamp>` files printed by --apply back over
/etc/mosquitto/opengrid.acl and /etc/mosquitto/opengrid.passwd (keep owner/mode: `cp -p`), then
`systemctl reload mosquitto`. The .mqtt.env files can stay; without the ACL/password entries they grant
nothing. Restoring the old ACL does NOT restore workspace access to ogtest/# unless that backup still had
the shared pattern line.
"""

from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import hmac
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

ACL_PATH = Path("/etc/mosquitto/opengrid.acl")
PASSWD_PATH = Path("/etc/mosquitto/opengrid.passwd")
WORK_DIR = Path("/opt/opengrid/work")
ENV_FILE_NAME = ".mqtt.env"
WORKSPACE_OWNER = "opengrid"
USER_PREFIX = "ogw_"
WORKSPACE_TOPIC_ROOT = "ogtest"
SYSTEMCTL = "/usr/bin/systemctl"
RUNUSER = "/usr/sbin/runuser"

BEGIN_MARKER = "# BEGIN ogw workspace users -- managed by deploy/mosquitto/provision_ws_users.py; do not edit"
END_MARKER = "# END ogw workspace users"

WORKSPACE_RE = re.compile(r"^[a-z0-9_]+$")
SHARED_PATTERN_RE = re.compile(r"^\s*pattern\s+readwrite\s+ogtest/#\s*$")
_ACL_LINE_RE = re.compile(
    r"^(user \S+|(topic|pattern) (read|write|readwrite|deny) \S+)$"
)

#: mosquitto 2.x password hashing: PBKDF2-HMAC-SHA512, 101 iterations, 12-byte salt, 64-byte digest,
#: stored as `$7$<iterations>$<b64 salt>$<b64 digest>` (verified against mosquitto_passwd 2.0.21).
PBKDF2_ITERATIONS = 101
SALT_BYTES = 12
DIGEST_BYTES = 64
PASSWORD_BYTES = 32


class ProvisionError(RuntimeError):
    """The plan cannot be applied safely; nothing has been changed."""


# --- pure helpers ---------------------------------------------------------------------------------------------


def workspace_user(ws: str) -> str:
    return f"{USER_PREFIX}{ws}"


def discover_workspaces(dir_names: Iterable[str], db_names: Iterable[str]) -> list[str]:
    """Workspace names from /opt/opengrid/work/<ws> directories and og_t_<ws> databases. Anything that is
    not a plain lowercase name (e.g. `<ws>.new` mid-sync) is ignored."""
    found = {name for name in dir_names if WORKSPACE_RE.match(name)}
    found |= {
        name.removeprefix("og_t_") for name in db_names if name.startswith("og_t_")
    }
    return sorted(ws for ws in found if WORKSPACE_RE.match(ws))


def hash_password(
    password: str, *, salt: bytes | None = None, iterations: int = PBKDF2_ITERATIONS
) -> str:
    salt = salt if salt is not None else secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha512", password.encode("utf-8"), salt, iterations, DIGEST_BYTES
    )
    return f"$7${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def password_matches(password: str, stored_hash: str) -> bool:
    try:
        _, alg, iterations, salt_b64, digest_b64 = stored_hash.split("$")
        if alg != "7":
            return False
        expected = hash_password(
            password, salt=base64.b64decode(salt_b64), iterations=int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expected.split("$")[-1], digest_b64)


def parse_passwd(text: str) -> list[tuple[str, str]]:
    entries = []
    for line in text.splitlines():
        if line.strip() and ":" in line:
            user, _, stored = line.partition(":")
            entries.append((user, stored))
    return entries


def render_passwd(entries: Sequence[tuple[str, str]]) -> str:
    return "".join(f"{user}:{stored}\n" for user, stored in entries)


def update_passwd(
    entries: Sequence[tuple[str, str]], new_hashes: dict[str, str]
) -> list[tuple[str, str]]:
    """Replace or append only the given users; every other entry (production users) stays as it is."""
    updated = [(user, new_hashes.get(user, stored)) for user, stored in entries]
    present = {user for user, _ in entries}
    updated += [
        (user, stored) for user, stored in new_hashes.items() if user not in present
    ]
    return updated


def strip_managed(acl_text: str) -> str:
    """The ACL without the managed block and without any shared `pattern readwrite ogtest/#` line."""
    kept: list[str] = []
    inside = False
    for line in acl_text.splitlines():
        if line.strip() == BEGIN_MARKER:
            inside = True
            continue
        if line.strip() == END_MARKER:
            inside = False
            continue
        if inside or SHARED_PATTERN_RE.match(line):
            continue
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept) + "\n"


def production_grants(acl_text: str) -> list[str]:
    """Every grant line outside the managed block, excluding the shared ogtest pattern -- i.e. the
    production users' rights, which the script must never change."""
    return [
        line.strip()
        for line in strip_managed(acl_text).splitlines()
        if _ACL_LINE_RE.match(line.strip())
    ]


def render_acl(current_acl: str, workspaces: Sequence[str]) -> str:
    block = [BEGIN_MARKER]
    for ws in workspaces:
        block += [
            f"user {workspace_user(ws)}",
            f"topic readwrite {WORKSPACE_TOPIC_ROOT}/{ws}/#",
            "",
        ]
    block.append(END_MARKER)
    return strip_managed(current_acl) + "\n" + "\n".join(block) + "\n"


def acl_users(acl_text: str) -> set[str]:
    return {
        line.split()[1]
        for line in acl_text.splitlines()
        if line.strip().startswith("user ")
    }


def validate(
    current_acl: str,
    new_acl: str,
    new_passwd: Sequence[tuple[str, str]],
    workspaces: Sequence[str],
) -> None:
    """Config check before anything is written. Raises ProvisionError."""
    for number, line in enumerate(new_acl.splitlines(), start=1):
        stripped = line.strip()
        if (
            stripped
            and not stripped.startswith("#")
            and not _ACL_LINE_RE.match(stripped)
        ):
            raise ProvisionError(f"ACL line {number} is not valid mosquitto ACL syntax")
    if any(SHARED_PATTERN_RE.match(line) for line in new_acl.splitlines()):
        raise ProvisionError("the shared ogtest/# pattern is still present")
    if production_grants(new_acl) != production_grants(current_acl):
        raise ProvisionError("production grants would change -- refusing")
    expected = {workspace_user(ws) for ws in workspaces}
    in_acl = {user for user in acl_users(new_acl) if user.startswith(USER_PREFIX)}
    in_passwd = {user for user, _ in new_passwd if user.startswith(USER_PREFIX)}
    if not expected <= in_acl or not expected <= in_passwd:
        raise ProvisionError(
            "a workspace user is missing from the ACL or the password file"
        )
    stale = (in_acl | in_passwd) - expected
    if stale & in_acl:
        raise ProvisionError(
            f"ACL grants ogw_ users with no workspace: {sorted(stale & in_acl)}"
        )


def read_env_file(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip():
            values[key.strip()] = value.strip()
    return values


def env_file_text(user: str, password: str) -> str:
    return (
        "# Workspace MQTT credentials (deploy/mosquitto/provision_ws_users.py). Never commit, never print.\n"
        f"OG_MQTT_WS_USER={user}\nOG_MQTT_WS_PASSWORD={password}\n"
    )


@dataclass
class Plan:
    workspaces: list[str]
    new_acl: str
    new_passwd: list[tuple[str, str]]
    env_files: dict[Path, str] = field(
        default_factory=dict
    )  # path -> content (holds a secret)
    user_actions: dict[str, str] = field(
        default_factory=dict
    )  # user -> "unchanged" | "add" | "rehash" | "rotate"


def build_plan(
    workspaces: Sequence[str],
    current_acl: str,
    current_passwd: str,
    work_dir: Path,
    *,
    new_password: Callable[[], str] | None = None,
) -> Plan:
    """Everything --apply would write, computed without touching disk. `new_password` is injectable for tests."""
    make_password = new_password or (lambda: secrets.token_urlsafe(PASSWORD_BYTES))
    stored = dict(parse_passwd(current_passwd))
    new_hashes: dict[str, str] = {}
    plan = Plan(list(workspaces), render_acl(current_acl, workspaces), [])
    for ws in workspaces:
        user = workspace_user(ws)
        env_path = work_dir / ws / ENV_FILE_NAME
        env = read_env_file(env_path)
        password = (
            env.get("OG_MQTT_WS_PASSWORD", "")
            if env.get("OG_MQTT_WS_USER") == user
            else ""
        )
        if password and user in stored and password_matches(password, stored[user]):
            plan.user_actions[user] = "unchanged"
            continue
        if password:
            plan.user_actions[user] = "rehash"
        else:
            password = make_password()
            plan.user_actions[user] = "rotate" if user in stored else "add"
            plan.env_files[env_path] = env_file_text(user, password)
        new_hashes[user] = hash_password(password)
    plan.new_passwd = update_passwd(parse_passwd(current_passwd), new_hashes)
    validate(current_acl, plan.new_acl, plan.new_passwd, workspaces)
    return plan


def describe(plan: Plan, current_acl: str) -> str:
    """The dry-run report: workspaces, per-user action, env files and the ACL diff. No secrets (the ACL
    and the file paths contain none; passwords and hashes are never included)."""
    lines = [
        f"workspaces ({len(plan.workspaces)}): {' '.join(plan.workspaces)}",
        "password file users:",
    ]
    lines += [
        f"  {user}: {action}" for user, action in sorted(plan.user_actions.items())
    ]
    lines.append("workspace env files to write (mode 0600, owner opengrid):")
    lines += [f"  {path}" for path in sorted(plan.env_files)] or ["  (none)"]
    lines.append("ACL diff:")
    diff = difflib.unified_diff(
        current_acl.splitlines(),
        plan.new_acl.splitlines(),
        str(ACL_PATH),
        f"{ACL_PATH} (new)",
        lineterm="",
    )
    lines += list(diff) or ["  (no change)"]
    lines.append(
        "production grants: "
        + (
            "unchanged"
            if production_grants(plan.new_acl) == production_grants(current_acl)
            else "CHANGED"
        )
    )
    return "\n".join(lines) + "\n"


# --- I/O (apply) ----------------------------------------------------------------------------------------------


def _db_workspaces() -> list[str]:
    result = subprocess.run(
        [
            "runuser",
            "-u",
            "postgres",
            "--",
            "psql",
            "-Atc",
            "SELECT datname FROM pg_database WHERE datname LIKE 'og_t_%'",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _backup(path: Path, stamp: str) -> Path:
    backup = path.with_name(f"{path.name}.bak.{stamp}")
    shutil.copy2(path, backup)
    st = path.stat()
    os.chown(backup, st.st_uid, st.st_gid)
    return backup


def _write_like(path: Path, text: str) -> None:
    """Atomically replace `path`, keeping its owner and mode."""
    st = path.stat()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, st.st_mode & 0o7777)
        os.chown(tmp, st.st_uid, st.st_gid)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _write_env_file(path: Path, text: str) -> None:
    import pwd  # POSIX-only; the pure planning helpers above stay importable anywhere

    owner = pwd.getpwnam(WORKSPACE_OWNER)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chown(path.parent, owner.pw_uid, owner.pw_gid)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)
    os.chown(path, owner.pw_uid, owner.pw_gid)


def _mosquitto_active() -> bool:
    return (
        subprocess.run(
            [SYSTEMCTL, "is-active", "--quiet", "mosquitto"], check=False
        ).returncode
        == 0
    )


def _reload() -> None:
    subprocess.run([SYSTEMCTL, "reload", "mosquitto"], check=True)


def apply(plan: Plan, current_acl: str) -> None:
    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    backups = {path: _backup(path, stamp) for path in (ACL_PATH, PASSWD_PATH)}
    for path, backup in backups.items():
        print(f"backup: {path} -> {backup}")
    for path, text in plan.env_files.items():
        _write_env_file(path, text)
    _write_like(PASSWD_PATH, render_passwd(plan.new_passwd))
    if plan.new_acl != current_acl:
        _write_like(ACL_PATH, plan.new_acl)
    _reload()
    time.sleep(2.0)
    if not _mosquitto_active():
        for path, backup in backups.items():
            shutil.copy2(backup, path)
        _reload()
        raise ProvisionError(
            "mosquitto was not active after reload; backups restored and reloaded"
        )
    print("applied; mosquitto reloaded and active")


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--workspaces", help="space-separated workspace names (default: discover)"
    )
    args = parser.parse_args(argv)

    if args.workspaces:
        workspaces = discover_workspaces(args.workspaces.split(), [])
    else:
        dirs = (
            [p.name for p in WORK_DIR.iterdir() if p.is_dir()]
            if WORK_DIR.is_dir()
            else []
        )
        workspaces = discover_workspaces(dirs, _db_workspaces())
    current_acl = ACL_PATH.read_text(encoding="utf-8")
    current_passwd = PASSWD_PATH.read_text(encoding="utf-8")
    try:
        plan = build_plan(workspaces, current_acl, current_passwd, WORK_DIR)
    except ProvisionError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(describe(plan, current_acl))
    if args.apply:
        if os.geteuid() != 0:
            print("--apply must run as root", file=sys.stderr)
            return 2
        apply(plan, current_acl)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
