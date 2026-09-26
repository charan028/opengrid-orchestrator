# tests-e2e/perf -- cycle-latency and UI-latency capture (WORKBOARD Q3, TS-N)

Owner: qa (`tests-e2e/`). Stdlib + httpx only; never imports `opengrid`.

| File | What |
|---|---|
| `metrics.py` | Pure: Prometheus text-exposition parser, `histogram_quantile` (PromQL algorithm), scrape deltas |
| `capture.py` | CLI: scrape `/metrics` N times, sample one SSE stream, write a Markdown PASS/FAIL report |
| `sample_metrics.txt` | Hand-written og-engine exposition (2k hubs, 300 cycles) for `--dry-run` and the tests |
| `test_metrics.py`, `test_capture.py` | Unit tests for everything pure, plus the dry-run CLI end to end |

Targets (02b S6.4 / S7.2): **RT cycle p99 < 500 ms at 2,000 hubs**, **UI event-to-screen <= 2 s**. The report also
fails if fewer than 95 % of the expected hubs are reporting (a p99 measured with no load is meaningless) or any
scrape failed.

## Run today, no system

```bash
orchestrator/.venv-local/bin/python tests-e2e/perf/capture.py --dry-run --report /tmp/perf-dry.md
orchestrator/.venv-local/bin/python -m pytest tests-e2e/perf -q
```

## Run locally at 2k (dev stack)

```bash
make dev-up                                    # WORKBOARD D0 (lead); Postgres + Mosquitto + og-* + sims
orchestrator/.venv-local/bin/python tests-e2e/perf/capture.py \
    --samples 60 --interval-s 2 \
    --metrics engine=http://127.0.0.1:9102/metrics --metrics guardian=http://127.0.0.1:9103/metrics \
    --api-base http://127.0.0.1:8080 --sse-path /og/api/stream/health --sse-frames 20 \
    --report tests-e2e/perf/reports/local-2k.md
```

`--samples 60 --interval-s 2` is a two-minute window (60 control cycles). The histogram quantiles are computed on
the *delta* between the first and last scrape, so the number is for the capture window, not the process lifetime.

Port convention: each process serves `prometheus_client.start_http_server` on `[metrics] bind_host` at
`metrics.<process>_port`. Only the guardian's is grounded in code today (`guardian/main.py`, default 9103); pass the
real ones with `--metrics name=url` once the other processes expose theirs (see `../chaos/NEEDS_FROM_OTHER_OWNERS.md`).
`health/metrics_scrape.py` uses the same plain-HTTP-GET convention.

## Run at 10k on the server (lead)

Load is controlled by `og-sim-fleet`'s `hub_count` in `integration-sims/config/fleet.yaml` (2,000 today; the systemd
unit's memory budget in `deploy/README.md` is sized for 2,000 -- raise `MemoryMax` on `og-sim-fleet.service` before
a 10k run, that is the deploy role's file). Do not edit `fleet.yaml` from this package; the lead changes it for the
run and reverts it.

```bash
# on basepower, as opengrid (metrics and the api are loopback-only)
python3 tests-e2e/perf/capture.py \
    --expected-hubs 10000 --samples 150 --interval-s 2 \
    --metrics engine=http://127.0.0.1:9102/metrics --metrics guardian=http://127.0.0.1:9103/metrics \
    --api-base http://127.0.0.1:8080 --remote-user viewer \
    --report /var/lib/opengrid/perf-10k.md
```

`--remote-user` sets the `X-Remote-User` header the SSE routes need (`api/auth.py`); `/og/api/health` itself is
loopback-exempt. The SSE read timeout is at least 20 s so the api's 15 s comment heartbeat never trips it.

## Reading the report

- **Checks** table: the verdict per target. `RT cycle p99` is `og_control_tick_duration_seconds{phase="total"}`
  (falls back to all phases aggregated if there is no `total` series).
- **Control cycle**: cycles observed in the window, mean/p50/p99, `og_control_ticks_total{outcome="late"}`, and
  `og_eventloop_lag_seconds` p99 (a high lag with a low tick p99 means the process is starved between ticks).
- **Fleet**: `og_hubs` by state and the worst-zone `og_telemetry_fresh_ratio`; if `online` is far below the expected
  count the load was not really there.
- **UI latency**: `timestamp` mode = wall clock minus the frame's own `as_of` (true event-to-screen, health stream);
  `gap` mode = inter-frame arrival interval, an upper bound used for streams without a clock field
  (`/og/api/stream/control-room`, `/og/api/stream/fleet`).

Exit code: 0 on PASS (always 0 in `--dry-run`), 1 on FAIL. Scrape failures are logged to stderr and counted in
the report; the run never aborts on one bad scrape.
