# Base server baseline evidence, 2026-09-26 (PERF lane)

These are the raw records behind the "baseline" section of report 17. They were collected on the base
server (192.168.5.35, production r3.4 `b1722df`, 3,509 hubs) before the owner moved the campaign to the
workstation.

- `guard.jsonl`: the watchdog's 15 s production samples, 22:06:42–22:15:21 CDT. Each sample has production
  cycle p99/max (the og-engine gauge on :9101), pgdata (dm-6) util and w_await from `iostat -dxy`, and fresh
  hubs from `/og/api/health`.
- `aborts.jsonl`: the one abort, at 22:08:13 (`pgdata_util_100.00%>50`).

Timeline (CDT):

| Time | Event |
|---|---|
| 21:10, 21:40 | `sar -d` 10-minute averages put production pgdata (dm-6) at 98.3 % and 99.8 % util. No perf work was running on base. |
| 21:57, 22:02 | The production og-engine `lifecycle` phase blocked the event loop for 4,237 ms and 5,384 ms. The engine's own "engine cycle latency" logs show these, with loop lag equal to the phase time. The pattern repeats about every 5 min, 3.5–5.4 s each. The PROD-IO lane attributed it to the PQ characterization loop. |
| 22:06:28 | `og_perf` bootstrapped on the 5433 test cluster (about 19 MB). |
| 22:06:30–22:08:16 | A 1,000-home (1,009 hub) perf stack ran. All hubs were online and perf cycle p99 was 50 ms. |
| 22:08:13 | The watchdog aborted the run on pgdata util of 100 %. Every perf unit was stopped by exact unit and PID within 3 s. |
| 22:09–22:15 | Production stayed saturated with no perf process running: pgdata 100 % util, w_await 100–740 ms, sda flush await 2.1–2.8 s. The test-cluster device showed 0 writes/s and dirty memory was 1.8 MB. Production cycle p99 was 2.9–4.7 s (max 14.7 s) and fresh hubs stayed at 3,509. |

The disk is a QEMU virtual HDD with a write-back cache, and the sda scheduler was `none`. So `ionice` and
`IOWeight` gave production no I/O protection; the lead changed it to bfq afterwards. The owner decided the
campaign runs on the workstation's Docker dev stack instead of base.
