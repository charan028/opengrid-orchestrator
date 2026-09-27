#!/usr/bin/env python3
"""deploy/k8s/images/k8s_support.py -- container-side helpers for the Kubernetes deployment (deploy/k8s/README.md).

The orchestrator's own code reads `orchestrator.toml` and trusts loopback; on Kubernetes the database, the broker
and the market simulator are separate Services, and kubelet probes arrive from outside the process. This module is
the one place that adapts a pod to that, without changing the application:

    render-config   write the release's orchestrator.toml with the chart's overrides (hosts, metrics bind) applied
    wait-db         block until Postgres accepts a connection with the orchestrator's DSN
    wait-schema     block until every migration shipped in the image is recorded in og.schema_migrations
    probe-http URL [STATUS]   exit 0 when URL answers with STATUS (default 200); for exec probes on loopback
    probe-heartbeat PROCESS   exit 0 when og.heartbeat has a fresh row for PROCESS (readiness only)
    exec-demo-customers       run dev/scripts/seed_demo_customers.py with its DSN taken from build_dsn

It never prints a credential: DSNs are built with opengrid.platform.db.build_dsn and passed only through the
environment of an exec'd child.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

RELEASE = Path(os.environ.get("OG_RELEASE", "/opt/opengrid/current"))
_SECTION_RE = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")
_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"')


# --- render-config ------------------------------------------------------------------------------------------


def _toml_value(value: Any) -> str:
    """Encode the scalar/list values the overrides use. json.dumps strings are valid TOML basic strings."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise SystemExit(f"render-config: unsupported override value type {type(value).__name__}")


def _flatten(table: dict[str, Any], prefix: str = "") -> list[tuple[str, str, Any]]:
    """{'postgres': {'host': 'x'}, 'api': {'roles': {...}}} -> [(section, key, value), ...]."""
    out: list[tuple[str, str, Any]] = []
    for key, value in table.items():
        if isinstance(value, dict):
            out.extend(_flatten(value, f"{prefix}.{key}" if prefix else key))
        elif not prefix:
            raise SystemExit(f"render-config: top-level override {key!r} must sit inside a [section]")
        else:
            out.append((prefix, key, value))
    return out


def _value_end(lines: list[str], start: int) -> int:
    """Index of the last line of the value that starts on lines[start] (multi-line arrays)."""
    depth = 0
    i = start
    while True:
        text = _STRING_RE.sub('""', lines[i]).split("#", 1)[0]
        depth += text.count("[") - text.count("]")
        if depth <= 0 or i + 1 >= len(lines):
            return i
        i += 1


def apply_overrides(base_text: str, overrides: dict[str, Any]) -> str:
    lines = base_text.splitlines()
    for section, key, value in _flatten(overrides):
        rendered = f"{key} = {_toml_value(value)}"
        header = next(
            (i for i, ln in enumerate(lines) if (m := _SECTION_RE.match(ln)) and m.group(1) == section), None
        )
        if header is None:
            lines += ["", f"[{section}]", rendered]
            continue
        end = next((i for i in range(header + 1, len(lines)) if _SECTION_RE.match(lines[i])), len(lines))
        key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
        hit = next((i for i in range(header + 1, end) if key_re.match(lines[i])), None)
        if hit is None:
            lines.insert(header + 1, rendered)
        else:
            lines[hit : _value_end(lines, hit) + 1] = [rendered]
    return "\n".join(lines) + "\n"


def _lookup(cfg: dict[str, Any], section: str, key: str) -> Any:
    node: Any = cfg
    for part in section.split("."):
        node = node.get(part, {}) if isinstance(node, dict) else {}
    return node.get(key) if isinstance(node, dict) else None


def cmd_render_config(args: argparse.Namespace) -> int:
    base = Path(args.base).read_text(encoding="utf-8")
    overrides_path = Path(args.overrides)
    overrides = tomllib.loads(overrides_path.read_text(encoding="utf-8")) if overrides_path.exists() else {}
    text = apply_overrides(base, overrides)
    rendered = tomllib.loads(text)  # the result must still be valid TOML ...
    for section, key, value in _flatten(overrides):  # ... and carry every override exactly
        if _lookup(rendered, section, key) != value:
            raise SystemExit(f"render-config: override [{section}].{key} did not apply")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    # Files next to orchestrator.toml (tdsp_tariffs.toml, authz.toml, grid/, service_profiles/) are resolved relative
    # to OG_CONFIG's directory: link them beside the rendered copy.
    base_path = Path(args.base)
    linked = 0
    for entry in sorted(base_path.parent.iterdir()):
        target = out.parent / entry.name
        if entry.name in (base_path.name, out.name) or target.exists() or target.is_symlink():
            continue
        target.symlink_to(entry)
        linked += 1
    n = len(_flatten(overrides))
    print(f"render-config: {out} ({n} override(s) from {overrides_path}, {linked} linked)")
    return 0


# --- database waits and probes ------------------------------------------------------------------------------


def _dsn() -> str:
    from opengrid.platform.config import load_config
    from opengrid.platform.db import build_dsn

    return str(build_dsn(load_config()))


def _connect() -> Any:
    import psycopg

    return psycopg.connect(_dsn(), autocommit=True, connect_timeout=5)


def _wait(label: str, ready: Any, timeout_s: float, interval_s: float = 5.0) -> int:
    deadline = time.monotonic() + timeout_s
    last = ""
    while True:
        try:
            ok, detail = ready()
        except Exception as exc:  # any failure means "not yet"; the reason is shown, not raised
            ok, detail = False, type(exc).__name__
        if ok:
            print(f"{label}: ready ({detail})")
            return 0
        if detail != last:
            print(f"{label}: waiting ({detail})", flush=True)
            last = detail
        if time.monotonic() >= deadline:
            print(f"{label}: timed out after {timeout_s:.0f}s ({detail})", file=sys.stderr)
            return 1
        time.sleep(interval_s)


def cmd_wait_db(args: argparse.Namespace) -> int:
    def ready() -> tuple[bool, str]:
        with _connect() as conn:
            conn.execute("SELECT 1")
        return True, "connected"

    return _wait("wait-db", ready, args.timeout)


def cmd_wait_schema(args: argparse.Namespace) -> int:
    from opengrid.platform.db import MIGRATIONS_DIR

    shipped = sorted(p.name for p in Path(MIGRATIONS_DIR).glob("[0-9][0-9][0-9][0-9]_*.sql"))

    def ready() -> tuple[bool, str]:
        with _connect() as conn:
            if not conn.execute("SELECT to_regclass('og.schema_migrations') IS NOT NULL").fetchone()[0]:
                return False, "og.schema_migrations absent"
            applied = {r[0] for r in conn.execute("SELECT filename FROM og.schema_migrations").fetchall()}
        missing = [f for f in shipped if f not in applied]
        return (not missing), f"{len(shipped) - len(missing)}/{len(shipped)} migrations applied"

    return _wait("wait-schema", ready, args.timeout)


def cmd_probe_http(args: argparse.Namespace) -> int:
    try:
        with urllib.request.urlopen(args.url, timeout=args.timeout) as resp:  # noqa: S310 -- fixed loopback URL
            code = resp.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    except (urllib.error.URLError, OSError) as exc:
        print(f"probe-http: {args.url}: {type(exc).__name__}", file=sys.stderr)
        return 1
    if code != args.status:
        print(f"probe-http: {args.url}: {code} (expected {args.status})", file=sys.stderr)
        return 1
    return 0


def cmd_probe_heartbeat(args: argparse.Namespace) -> int:
    from opengrid.platform.config import load_config

    cfg = load_config()
    interval = float(cfg.get("health.heartbeat_interval_s", 5))
    max_age = interval * (int(cfg.get("health.heartbeat_miss_threshold", 3)) + 1)
    with _connect() as conn:
        row = conn.execute(
            "SELECT extract(epoch FROM now() - ts) FROM og.heartbeat WHERE process = %s", (args.process,)
        ).fetchone()
    if row is None or float(row[0]) > max_age:
        state = "no row" if row is None else f"{float(row[0]):.0f}s old"
        print(f"probe-heartbeat: {args.process}: {state} (limit {max_age:.0f}s)", file=sys.stderr)
        return 1
    return 0


def cmd_exec_demo_customers(args: argparse.Namespace) -> int:
    env = dict(os.environ)
    env["OG_SEED_DSN"] = _dsn()
    env.setdefault("OG_SEED_API", "http://127.0.0.1:8080")
    script = RELEASE / "dev" / "scripts" / "seed_demo_customers.py"
    os.execve(sys.executable, [sys.executable, str(script), *args.rest], env)  # noqa: S606 -- the release's script
    return 1  # unreachable


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    rc = sub.add_parser("render-config")
    rc.add_argument("--base", required=True)
    rc.add_argument("--overrides", required=True)
    rc.add_argument("--out", required=True)
    rc.set_defaults(func=cmd_render_config)
    for name, func in (("wait-db", cmd_wait_db), ("wait-schema", cmd_wait_schema)):
        w = sub.add_parser(name)
        w.add_argument("--timeout", type=float, default=float(os.environ.get("OG_WAIT_TIMEOUT_S", "1800")))
        w.set_defaults(func=func)
    ph = sub.add_parser("probe-http")
    ph.add_argument("url")
    ph.add_argument("status", nargs="?", type=int, default=200)
    ph.add_argument("--timeout", type=float, default=4.0)
    ph.set_defaults(func=cmd_probe_http)
    hb = sub.add_parser("probe-heartbeat")
    hb.add_argument("process")
    hb.set_defaults(func=cmd_probe_heartbeat)
    dc = sub.add_parser("exec-demo-customers")
    dc.add_argument("rest", nargs=argparse.REMAINDER)
    dc.set_defaults(func=cmd_exec_demo_customers)
    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
