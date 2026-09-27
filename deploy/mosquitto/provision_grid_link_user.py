#!/usr/bin/env python3
"""MQTT user `og_gridlink` for the D-34 grid link (grid-link.md S7): publish ONLY on
`<root>/scada/instruction/#`. Called by deploy/scripts/grid_link_enable_loopback.sh; reuses the hashing,
backup, atomic-write and reload helpers of provision_ws_users.py (one owner for broker-file edits).

    GL_MQTT_PASSWORD=... provision_grid_link_user.py --dry-run|--apply [--topic-root og/v1]

The password comes ONLY from the environment variable GL_MQTT_PASSWORD (never argv, never printed). What
--apply does (idempotent): back up both broker files as `<file>.bak.<UTC stamp>` (owner/mode kept); set the
`og_gridlink` hash in the password file (kept when it already matches); add one managed ACL block (before
the workspace block, if any); write atomically; `systemctl reload mosquitto`; if mosquitto is not active
afterwards, restore the backups and reload again. Every other user's entries and grants stay byte-identical.

Rollback: `cp -p` the printed .bak files back and `systemctl reload mosquitto`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import provision_ws_users as ws

USER = "og_gridlink"
BEGIN = "# BEGIN og_gridlink -- managed by deploy/mosquitto/provision_grid_link_user.py; do not edit"
END = "# END og_gridlink"


def strip_block(text: str) -> list[str]:
    """The ACL lines outside the og_gridlink block. Pure."""
    kept: list[str] = []
    inside = after_end = False
    for line in text.splitlines():
        if line.strip() == BEGIN:
            inside = True
            continue
        if line.strip() == END:
            inside, after_end = False, True
            continue
        if not inside and not (after_end and not line.strip()):
            kept.append(line)
        after_end = False
    return kept


def other_grants_unchanged(before: str, after: str) -> bool:
    """Every non-blank line outside the og_gridlink block is identical and in the same order."""
    return [x for x in strip_block(before) if x.strip()] == [x for x in strip_block(after) if x.strip()]


def render_acl(current: str, topic_root: str) -> str:
    """`current` with exactly one og_gridlink block (write-only on scada/instruction/#). Pure."""
    kept = strip_block(current)
    block = [BEGIN, f"user {USER}", f"topic write {topic_root.rstrip('/')}/scada/instruction/#", END, ""]
    if ws.BEGIN_MARKER in kept:
        at = kept.index(ws.BEGIN_MARKER)
        kept[at:at] = block
    else:
        while kept and not kept[-1].strip():
            kept.pop()
        kept += ["", *block]
    return "\n".join(kept).rstrip("\n") + "\n"


def plan_passwd(current: str, password: str) -> tuple[str, bool]:
    """(new password-file text, changed?). Keeps a matching hash, so re-running changes nothing."""
    entries = ws.parse_passwd(current)
    stored = dict(entries).get(USER)
    if stored is not None and ws.password_matches(password, stored):
        return current, False
    return ws.render_passwd(ws.update_passwd(entries, {USER: ws.hash_password(password)})), True


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--topic-root", default="og/v1")
    args = parser.parse_args(argv)
    password = os.environ.get("GL_MQTT_PASSWORD", "")
    if len(password) < 16:
        print("GL_MQTT_PASSWORD is missing or too short", file=sys.stderr)
        return 2
    acl, passwd = ws.ACL_PATH.read_text(encoding="utf-8"), ws.PASSWD_PATH.read_text(encoding="utf-8")
    new_acl = render_acl(acl, args.topic_root)
    new_passwd, passwd_changed = plan_passwd(passwd, password)
    if not other_grants_unchanged(acl, new_acl):
        print("refusing: the ACL change would touch other users' grants", file=sys.stderr)
        return 2
    print(f"acl: {'unchanged' if new_acl == acl else 'og_gridlink block added/updated'}")
    print(f"passwd: {'og_gridlink hash set' if passwd_changed else 'og_gridlink hash already matches'}")
    if args.dry_run or (new_acl == acl and not passwd_changed):
        return 0
    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    backups = {path: ws._backup(path, stamp) for path in (ws.ACL_PATH, ws.PASSWD_PATH)}
    for path, backup in backups.items():
        print(f"backup: {path} -> {backup}")
    ws._write_like(ws.PASSWD_PATH, new_passwd)
    ws._write_like(ws.ACL_PATH, new_acl)
    ws._reload()
    time.sleep(2.0)
    if not ws._mosquitto_active():
        for path, backup in backups.items():
            shutil.copy2(backup, path)
        ws._reload()
        print("mosquitto was not active after reload; backups restored and reloaded", file=sys.stderr)
        return 1
    print("applied; mosquitto reloaded and active")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
