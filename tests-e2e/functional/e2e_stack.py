"""Black-box client for the running dev stack (dev/README.md, WP D0), shared by the Q1/Q2 functional suites.

Tests act through the operator API and the simulator control plane, the way an operator or an external system
would, and read `og.*` rows only to assert on what the running processes did. The one exception is
`offer()`, which sets `og.opportunity.value_per_mwh` right after admission: the API has no field for it (the
intake prices its own offers from the forecast), and an unpriced market offer is correctly never selected.

Configuration (all optional; the defaults match `dev/docker-compose.yml` and read `dev/secrets`):

    OG_E2E_API            http://127.0.0.1:8080/og/api
    OG_E2E_CONTROL        http://127.0.0.1:8091
    OG_E2E_DSN            postgresql://opengrid:<OG_DB_PASSWORD>@127.0.0.1:<POSTGRES_PORT from dev/.env>/og
    OG_E2E_PROXY_SECRET   <OG_API_PROXY_SECRET>   (og-api only trusts identity headers carrying it)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

REPO_ROOT = Path(__file__).resolve().parents[2]
DEV_SECRETS = REPO_ROOT / "dev" / "secrets"
DEV_ENV = REPO_ROOT / "dev" / ".env"
INTERVAL = timedelta(minutes=15)
#: Present only on a deployed OpenGrid host. There the defaults below are the PRODUCTION og-api, simulator
#: control plane and database, so the suites refuse to run unless every target is set explicitly.
PRODUCTION_HOST_MARKER = Path("/etc/opengrid")
#: Regulated-utility territories (K15): a manual operator command is FREE-market work, which G-33 correctly vetoes
#: on these hubs, so generic guardian scenarios pick competitive-area hubs. The `market = "REGULATED"` zones of
#: `[zone_territory]` (orchestrator/config/tdsp_tariffs.toml), LZ_LCRA and LZ_RAYBN since D-37.
REGULATED_ZONES = ("LZ_AEN", "LZ_CPS", "LZ_LCRA", "LZ_RAYBN")
#: The `slow` marker promises a delivery scenario waits at most about this long for its window (conftest.py).
MAX_DELIVERY_WAIT = timedelta(minutes=20)
EXPLICIT_TARGETS = ("OG_E2E_API", "OG_E2E_CONTROL", "OG_E2E_DSN")

#: How long an admission gate normally takes to decide a fresh contract's offer (next 2 s engine tick + solve).
GATE_TIMEOUT_S = 90.0
#: No ADMISSION plan for this long after the offer's own gate means the gate queue has drained.
GATE_QUIET = timedelta(seconds=4)


def _dev_value(path: Path, name: str, default: str = "") -> str:
    if not path.exists():
        return default
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == name and value.strip():
            return value.strip()
    return default


def _dev_secret(name: str) -> str:
    return _dev_value(DEV_SECRETS, name)


def now_utc() -> datetime:
    return datetime.now(UTC)


def quarter(offset: int = 1, *, at: datetime | None = None) -> datetime:
    """The 15-minute interval boundary `offset` intervals after the current one (1 = the next boundary)."""
    at = at or now_utc()
    floored = at.replace(minute=at.minute - at.minute % 15, second=0, microsecond=0)
    return floored + INTERVAL * offset


def wait_until[T](
    probe: Callable[[], T | None],
    *,
    timeout_s: float,
    interval_s: float = 1.0,
    what: str,
) -> T:
    """Poll `probe` until it returns something truthy; fail with `what` on timeout (never a bare sleep)."""
    deadline = time.monotonic() + timeout_s
    last: T | None = None
    while time.monotonic() < deadline:
        last = probe()
        if last:
            return last
        time.sleep(interval_s)
    # One last look: after a host sleep the monotonic deadline can pass while the condition already holds.
    last = probe()
    if last:
        return last
    raise AssertionError(f"timed out after {timeout_s:.0f}s waiting for {what} (last={last!r})")


@dataclass(frozen=True)
class Offer:
    contract_id: UUID
    opportunity_id: UUID
    window_start: datetime
    window_end: datetime
    requested_kw: Decimal


class Stack:
    def __init__(self) -> None:
        self.api_base = os.environ.get("OG_E2E_API", "http://127.0.0.1:8080/og/api").rstrip("/")
        self.control_base = os.environ.get("OG_E2E_CONTROL", "http://127.0.0.1:8091").rstrip("/")
        password = _dev_secret("OG_DB_PASSWORD")
        port = _dev_value(DEV_ENV, "POSTGRES_PORT", "5432")
        self.dsn = os.environ.get("OG_E2E_DSN", f"postgresql://opengrid:{password}@127.0.0.1:{port}/og")
        secret = os.environ.get("OG_E2E_PROXY_SECRET") or _dev_secret("OG_API_PROXY_SECRET")
        self._secret = secret
        self.http = httpx.Client(timeout=30.0)
        self.created_contracts: list[UUID] = []

    # --- transport ---------------------------------------------------------------------------------

    def headers(self, user: str = "operator") -> dict[str, str]:
        return {"X-Remote-User": user, "X-OG-Proxy-Auth": self._secret}

    def get(self, path: str, *, user: str = "operator", **params: Any) -> httpx.Response:
        return self.http.get(f"{self.api_base}{path}", headers=self.headers(user), params=params)

    def post(
        self, path: str, body: dict[str, Any] | None = None, *, user: str = "operator"
    ) -> httpx.Response:
        return self.http.post(f"{self.api_base}{path}", headers=self.headers(user), json=body or {})

    def patch(self, path: str, body: dict[str, Any], *, user: str = "operator") -> httpx.Response:
        return self.http.patch(f"{self.api_base}{path}", headers=self.headers(user), json=body)

    def control(self, method: str, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
        # R3: the sim control plane refuses a state-changing request without this CSRF marker (403).
        return self.http.request(
            method, f"{self.control_base}{path}", json=body, headers={"X-OGSim-Request": "1"}
        )

    def rows(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            return list(conn.execute(sql, params or {}).fetchall())

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute(sql, params or {})

    def reachable(self) -> str | None:
        """None when the stack answers, else why not (used to skip the whole suite cleanly)."""
        if PRODUCTION_HOST_MARKER.exists():
            missing = [name for name in EXPLICIT_TARGETS if not os.environ.get(name)]
            if missing:
                return (
                    f"refusing the default targets on a production host ({PRODUCTION_HOST_MARKER} exists); "
                    f"set {', '.join(missing)} to a dev stack"
                )
        try:
            resp = self.get("/fleet/summary")
        except httpx.HTTPError as exc:
            return f"og-api not reachable at {self.api_base}: {exc}"
        if resp.status_code != 200:
            return f"og-api answered {resp.status_code} (proxy secret or identity config?)"
        try:
            self.rows("SELECT 1")
        except psycopg.Error as exc:
            return f"Postgres not reachable: {exc}"
        return None

    # --- arrange: contracts and offers -------------------------------------------------------------

    def create_contract(self, service_type: str, tier: str, *, variant: str | None = None) -> UUID:
        body = {
            "customer_id": str(uuid4()),
            "service_type": service_type,
            "variant": variant,
            "tier": tier,
            "profile_ref": f"e2e-{service_type.lower()}@1",
            "start_at": (now_utc() - timedelta(days=1)).isoformat(),
            "renomination_allowed": False,
            "penalty_alpha": "0.01",
            "penalty_beta": "0.25",
            "penalty_theta": "0.10",
        }
        resp = self.post("/contracts", body)
        assert resp.status_code == 201, resp.text
        contract_id = UUID(resp.json()["contract_id"])
        self.created_contracts.append(contract_id)
        return contract_id

    def add_product_rule(
        self,
        contract_id: UUID,
        *,
        product_code: str,
        min_qty_kw: float,
        increment_kw: float,
        block: bool,
        duration_minutes: int,
        variable_kind: str,
    ) -> None:
        self.execute(
            """INSERT INTO og.product_rule (product_rule_id, contract_id, product_code, min_qty_kw, increment_kw,
                                            block, duration_minutes, variable_kind)
               VALUES (%(id)s, %(c)s, %(code)s, %(min)s, %(inc)s, %(block)s, %(dur)s, %(kind)s)""",
            {
                "id": uuid4(),
                "c": contract_id,
                "code": product_code,
                "min": min_qty_kw,
                "inc": increment_kw,
                "block": block,
                "dur": duration_minutes,
                "kind": variable_kind,
            },
        )

    def offer(
        self,
        contract_id: UUID,
        *,
        window_start: datetime,
        window_end: datetime,
        requested_kw: float,
        value_per_mwh: float,
    ) -> Offer:
        resp = self.post(
            "/opportunities",
            {
                "contract_id": str(contract_id),
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "requested_kw": str(requested_kw),
            },
        )
        assert resp.status_code == 201, resp.text
        opportunity_id = UUID(resp.json()["opportunity_id"])
        self.execute(
            "UPDATE og.opportunity SET value_per_mwh = %(v)s WHERE opportunity_id = %(o)s",
            {"v": value_per_mwh, "o": opportunity_id},
        )
        return Offer(
            contract_id,
            opportunity_id,
            window_start,
            window_end,
            Decimal(str(requested_kw)),
        )

    def end_contract(self, contract_id: UUID) -> None:
        self.patch(f"/contracts/{contract_id}", {"status": "ENDED"})

    # --- observe ------------------------------------------------------------------------------------

    def obligation(self, offer: Offer) -> dict[str, Any] | None:
        found = self.rows(
            "SELECT * FROM og.obligation WHERE opportunity_id = %(o)s",
            {"o": offer.opportunity_id},
        )
        return found[0] if found else None

    def wait_decided(self, offer: Offer, *, timeout_s: float = GATE_TIMEOUT_S) -> dict[str, Any]:
        """Wait until the selector has decided the offer. A selected (or rejected) offer leaves OFFERED; an
        offer the gate declined for lack of headroom correctly stays OFFERED with nothing recorded against
        it, so that case is recognised by an ADMISSION plan written after the offer followed by a quiet
        gate queue (gates run one at a time, ~0.1 s each)."""

        def decided() -> dict[str, Any] | None:
            ob = self.obligation(offer)
            if ob is None:
                return None
            if ob["state"] != "OFFERED":
                return ob
            plans = self.rows(
                """SELECT max(p.created_at) AS last_plan, now() AS db_now FROM og.plan p
                   WHERE p.gate_kind = 'ADMISSION'
                     AND p.created_at > (SELECT admitted_at FROM og.opportunity WHERE opportunity_id = %(o)s)""",
                {"o": offer.opportunity_id},
            )[0]
            if plans["last_plan"] and plans["db_now"] - plans["last_plan"] > GATE_QUIET:
                return ob
            return None

        return wait_until(
            decided,
            timeout_s=timeout_s,
            what=f"a gate decision on opportunity {offer.opportunity_id}",
        )

    def delivery_window(
        self, *, max_committed_kw: float, lead: timedelta = timedelta(seconds=90)
    ) -> tuple[datetime, datetime]:
        """The earliest lightly-loaded single interval that opens at least `lead` from now, for a scenario that
        waits for a real delivery. Skips, naming the committed load it found, when that interval is further
        away than MAX_DELIVERY_WAIT: on a loaded stack `light_window` can land hours out, and the `slow` marker
        promises about 20 minutes."""
        earliest = now_utc() + lead
        start, end = self.light_window(1, max_committed_kw=max_committed_kw)
        if start < earliest:
            start, end = self.light_window(1, max_committed_kw=max_committed_kw, first_offset=2)
        if start - now_utc() > MAX_DELIVERY_WAIT:
            load = self.rows(
                """SELECT c.interval_start, sum(c.committed_kw) AS kw FROM og.commitment c
                   WHERE c.interval_start >= %(a)s AND c.interval_start < %(b)s
                     AND NOT EXISTS (SELECT 1 FROM og.commitment n WHERE n.supersedes = c.commitment_id)
                   GROUP BY 1 ORDER BY 1""",
                {"a": quarter(0), "b": start},
            )
            summary = ", ".join(f"{r['interval_start']:%H:%M} {r['kw']} kW" for r in load) or "none"
            pytest.skip(
                f"no interval with <= {max_committed_kw} kW committed opens within {MAX_DELIVERY_WAIT}; the nearest "
                f"is {start:%H:%M} UTC. Committed load before it: {summary}. Reset the dev DB or wait."
            )
        return start, end

    def cleanup(self) -> None:
        """Dev stack only: retire what this session created so reruns start clean. Every non-superseded
        commitment row stays frozen for the selector (K13) whatever the obligation's state, so this deletes the
        session's FUTURE commitment rows, releases their reservations and expires obligations that had not
        started, then ends the contracts. Past and in-progress intervals are left as history."""
        if PRODUCTION_HOST_MARKER.exists() or not self.created_contracts:
            for contract_id in self.created_contracts:
                self.end_contract(contract_id)
            return
        ids = list(self.created_contracts)
        with psycopg.connect(self.dsn) as conn:
            obligations = [
                row[0]
                for row in conn.execute(
                    "SELECT obligation_id FROM og.obligation WHERE contract_id = ANY(%(c)s)", {"c": ids}
                ).fetchall()
            ]
            if obligations:
                conn.execute(
                    "DELETE FROM og.commitment WHERE obligation_id = ANY(%(o)s) AND interval_start > now()",
                    {"o": obligations},
                )
                conn.execute(
                    "UPDATE og.reservation SET released_at = now(), release_reason = 'E2E_CLEANUP' "
                    "WHERE obligation_id = ANY(%(o)s) AND released_at IS NULL AND interval_start > now()",
                    {"o": obligations},
                )
                conn.execute(
                    "UPDATE og.obligation SET state = 'EXPIRED', last_reason_code = 'E2E_CLEANUP', updated_at = now() "
                    "WHERE obligation_id = ANY(%(o)s) AND window_start > now() "
                    "AND state IN ('OFFERED', 'SELECTED', 'COMMITTED')",
                    {"o": obligations},
                )
        for contract_id in ids:
            self.end_contract(contract_id)

    def light_window(
        self, intervals: int, *, max_committed_kw: float, first_offset: int = 1
    ) -> tuple[datetime, datetime]:
        """The earliest run of `intervals` intervals whose total live commitments stay at or under
        `max_committed_kw` -- for scenarios that need a delivery window soon, when `free_window` would be
        hours away because earlier runs left small commitments in every nearer interval."""
        base = quarter(0)
        load = {
            row["interval_start"]: float(row["kw"])
            for row in self.rows(
                """SELECT c.interval_start, sum(c.committed_kw) AS kw FROM og.commitment c
                   WHERE c.interval_start >= %(a)s
                     AND NOT EXISTS (SELECT 1 FROM og.commitment n WHERE n.supersedes = c.commitment_id)
                   GROUP BY 1""",
                {"a": base},
            )
        }
        for offset in range(first_offset, 88 - intervals):
            window = [base + INTERVAL * (offset + i) for i in range(intervals)]
            if all(load.get(start, 0.0) <= max_committed_kw for start in window):
                return window[0], window[-1] + INTERVAL
        raise AssertionError(f"no {intervals} intervals with <= {max_committed_kw} kW committed")

    def compose(self, *args: str) -> None:
        """Run `docker compose` against the dev stack (process-kill scenarios). Skips when docker is absent."""
        docker = shutil.which("docker")
        if docker is None:
            pytest.skip("docker CLI not available for a process-kill scenario")
        compose_file = str(REPO_ROOT / "dev" / "docker-compose.yml")
        subprocess.run(  # noqa: S603 -- fixed argv, resolved docker binary, test-authored service names only
            [docker, "compose", "-f", compose_file, "--profile", "orchestrator", *args],
            check=True,
            capture_output=True,
            timeout=180,
        )

    def metric(self, service: str, port: int, name: str) -> float | None:
        """Sum of a Prometheus counter/gauge `name` scraped from a dev-stack container's loopback-only
        `/metrics` (via `docker compose exec`, since the endpoint never binds a published port). `None` when
        the endpoint or the series is absent. A declared counter with no sample yet counts as 0: a labelled
        counter (e.g. `og_mqtt_reconnects_total{client}`) shows no series until its first increment."""
        docker = shutil.which("docker")
        if docker is None:
            pytest.skip("docker CLI not available to scrape a loopback metrics endpoint")
        script = (
            "import urllib.request; "
            f"print(urllib.request.urlopen('http://127.0.0.1:{port}/metrics', timeout=5).read().decode())"
        )
        compose_file = str(REPO_ROOT / "dev" / "docker-compose.yml")
        done = subprocess.run(  # noqa: S603 -- fixed argv, test-authored service name and script
            [docker, "compose", "-f", compose_file, "exec", "-T", service, "python", "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if done.returncode != 0:
            return None
        values = [
            float(line.rsplit(" ", 1)[1])
            for line in done.stdout.splitlines()
            if line.startswith(name) and not line.startswith("#")
        ]
        if not values and f"# TYPE {name} counter" in done.stdout:
            return 0.0
        return sum(values) if values else None

    def restart_count(self, service: str) -> int:
        """How many times Docker has restarted a dev-stack service's container (the systemd-restart analogue)."""
        docker = shutil.which("docker")
        if docker is None:
            pytest.skip("docker CLI not available")
        done = subprocess.run(  # noqa: S603 -- fixed argv
            [docker, "inspect", "-f", "{{.RestartCount}}", f"dev-{service}-1"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return int(done.stdout.strip() or 0)

    def heartbeat_age_s(self, process: str) -> float | None:
        found = self.rows(
            "SELECT extract(epoch FROM now() - ts) AS age FROM og.heartbeat WHERE process = %(p)s",
            {"p": process},
        )
        return float(found[0]["age"]) if found else None

    def wait_process_up(self, process: str, *, timeout_s: float = 90.0) -> None:
        wait_until(
            lambda: (age := self.heartbeat_age_s(process)) is not None and age < 5.0,
            timeout_s=timeout_s,
            what=f"a fresh {process} heartbeat",
        )

    def manual_command(self, hub_id: str, p_kw: float, *, user: str = "operator") -> httpx.Response:
        """Operator manual setpoint, both steps (02b S7.3): the confirm answer carries the guardian's verdict."""
        proposed = self.post(
            "/fleet/command", {"hub_id": hub_id, "p_kw_setpoint": p_kw, "reason": "e2e"}, user=user
        )
        assert proposed.status_code == 202, proposed.text
        return self.post(f"/fleet/command/{proposed.json()['proposal_id']}/confirm", user=user)

    def bank_verdicts(self, bank_id: str, since: datetime) -> list[dict[str, Any]]:
        """The guardian's verdicts on this bank's engine batches created since `since` (R3: manual commands
        ramp through the engine, so every step to a bank shows up here, not as a per-command verdict)."""
        return self.rows(
            """SELECT v.outcome, v.vetoed_rule_ids FROM og.verdict v JOIN og.command_batch b USING (command_batch_id)
               WHERE b.created_at >= %(t)s AND b.submission_id LIKE %(sub)s""",
            {"t": since, "sub": f"%{bank_id}%"},
        )

    def online_hub(self, *, exclude_banks: tuple[str, ...] = (), idle: bool = False) -> dict[str, Any]:
        """An online hub (params + live state) in the ERCOT competitive area, outside `exclude_banks`. `idle=True` wants one at 0 kW, i.e.
        not currently driven by the engine, and skips the test when every hub is being dispatched (the
        engine also dispatches uncommitted headroom, so on a busy stack there may be none)."""
        found = self.rows(
            """SELECT h.hub_id, h.bank_id, h.p_kw AS p_limit_kw, h.e_kwh, h.r_kwh, s.soc_kwh, s.p_kw
               FROM og.hub h JOIN og.hub_state s USING (hub_id)
               JOIN og.bank b ON b.bank_id = h.bank_id
               WHERE s.health = 'online' AND NOT (h.bank_id = ANY(%(x)s))
                 AND NOT (b.zone = ANY(%(reg)s))
                 AND (NOT %(idle)s OR s.p_kw = 0)
               ORDER BY s.soc_kwh DESC LIMIT 1""",
            {"x": list(exclude_banks), "idle": idle, "reg": list(REGULATED_ZONES)},
        )
        if not found and idle:
            pytest.skip("every online hub is currently dispatched by the engine; no idle hub to command")
        assert found, "no online hub available"
        return found[0]

    def inject(self, anomaly: str, target: str, *, duration_s: float = 60.0, **params: Any) -> str:
        """Inject a simulator anomaly through `ogsim.control` (the same path as its web UI); returns its id."""
        resp = self.control(
            "POST",
            "/api/inject",
            {"type": anomaly, "target": target, "params": params, "duration": duration_s},
        )
        assert resp.status_code == 200, resp.text
        return str(resp.json()["anomaly"]["id"])

    def clear_anomaly(self, anomaly_id: str) -> None:
        self.control("DELETE", f"/api/anomalies/{anomaly_id}")

    def require_committed(
        self, obligation: dict[str, Any], what: str = "the scenario's baseline offer"
    ) -> None:
        """Skip, not fail, when the stack cannot commit at all: since R2 the selector withholds firm commitments
        from banks without a zone price forecast (NOT_FOR_FIRM) and while NO_NEW_COMMITMENTS is active, which a
        fresh dev database or a stale feed produces. A scenario can only test the lock once something commits."""
        if obligation["state"] == "COMMITTED":
            return
        modes = [row["mode"] for row in self.rows("SELECT mode FROM og.degraded_mode_state")]
        pytest.skip(
            f"{what} was not committed ({obligation['state']}); degraded modes: {modes or 'none'}. The selector "
            "withholds banks without a zone price forecast (NOT_FOR_FIRM, ~3 days of history on a fresh DB): "
            "backfill it with orchestrator/tools/ercot_backfill.py (see its --status/--dry-run) before this suite"
        )

    def free_window(
        self, intervals: int, *, first_offset: int = 3, last_offset: int = 88
    ) -> tuple[datetime, datetime]:
        """The latest run of `intervals` consecutive 15-minute intervals (inside the selector's 24 h horizon)
        with no live commitment at all, so a scenario's capacity is never taken by an earlier run's (still
        locked, K13) obligations. Latest, not earliest: a gate-only scenario's commitments (a rival may take
        the whole fleet) then deliver many hours later, never during a session's delivery or manual-command
        scenarios."""
        base = quarter(0)
        taken = {
            row["interval_start"]
            for row in self.rows(
                """SELECT DISTINCT c.interval_start FROM og.commitment c
                   WHERE c.interval_start >= %(a)s
                     AND NOT EXISTS (SELECT 1 FROM og.commitment n WHERE n.supersedes = c.commitment_id)""",
                {"a": base},
            )
        }
        for offset in range(last_offset - intervals, first_offset - 1, -1):
            window = [base + INTERVAL * (offset + i) for i in range(intervals)]
            if not taken.intersection(window):
                return window[0], window[-1] + INTERVAL
        raise AssertionError(
            f"no {intervals} free intervals in the next {last_offset} (reset the dev database?)"
        )

    def wait_state(self, offer: Offer, states: set[str], *, timeout_s: float) -> dict[str, Any]:
        return wait_until(
            lambda: ob if (ob := self.obligation(offer)) and ob["state"] in states else None,
            timeout_s=timeout_s,
            what=f"opportunity {offer.opportunity_id} to reach {sorted(states)}",
        )

    def active_commitments(self, obligation_id: UUID) -> dict[datetime, Decimal]:
        """interval_start -> committed kW for the obligation's live commitment rows (a row another row
        supersedes is history, not the current commitment)."""
        found = self.rows(
            """SELECT c.interval_start, c.committed_kw FROM og.commitment c
               WHERE c.obligation_id = %(ob)s
                 AND NOT EXISTS (SELECT 1 FROM og.commitment n WHERE n.supersedes = c.commitment_id)""",
            {"ob": obligation_id},
        )
        return {row["interval_start"]: row["committed_kw"] for row in found}

    def commitment_history(self, obligation_id: UUID) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT * FROM og.commitment WHERE obligation_id = %(ob)s ORDER BY created_at",
            {"ob": obligation_id},
        )

    def trace_reason_codes(self, since: datetime) -> list[str]:
        found = self.rows(
            "SELECT reason_codes FROM og.trace WHERE created_at >= %(t)s AND reason_codes IS NOT NULL",
            {"t": since},
        )
        return [code for row in found for code in row["reason_codes"]]

    def iter_created_contracts(self) -> Iterator[UUID]:
        yield from self.created_contracts
