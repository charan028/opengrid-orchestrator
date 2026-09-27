# The five-minute cut

For the judged video. Everything below runs on the dev stack with `og-feeds` reading the simulator, so
the price spike is injectable. Each row is one thing on screen and one thing the judge learns. Keep the
Story screen open in a second tab; it is where the promise counters live.

| Time | On screen | What the judge learns |
|---|---|---|
| 0:00 | **Story** (`/og/story`), the top of the page | One fleet, many buyers, homes first. Read the thesis aloud; it is one sentence. |
| 0:25 | Story, "Right now" | N homes reporting, promised kW across N obligations for N buyers. The table shows who holds a claim. |
| 0:45 | **Control room** map | Real transmission lines, real load zones, every home placed. The orange line is the best sell destination this hour. |
| 1:10 | Inject the spike: `demo-01-price-spike-lock` on `/ogsim/` | Every load-zone price jumps to $5,000/MWh on the ticker. |
| 1:30 | **Dispatch**, the committed cards | They do not move. Each still reads "Locked (K13)". The fleet is not chasing the price. |
| 1:50 | Story, "The three promises" | All three counters still read 0, live. |
| 2:05 | **Profitability**, "Forgone upside (lock)" | It turned non-zero. This is the money the fleet declined so it could keep its word, and it is on the books. |
| 2:30 | Copilot, ask "why didn't we chase the spike?" | Answered from console data with citations: the lock, the reason code, the dollars forgone. |
| 3:00 | **Dispatch**, "AS awards & deployment" | An ERCOT ancillary-services award sits at 0 kW with its energy held above the homes' reserve. |
| 3:15 | Press **Deploy**, then **Confirm deployment** | Two steps, a countdown, a signed batch. From the next cycle it dispatches like any delivery. |
| 3:45 | **Fleet**, safe stop on one bank | Propose, confirm. The bank ramps to zero. The engine is not involved; this path has its own key. |
| 4:05 | Release it: request, then a second operator approves | Two people, checked twice, in the trace. |
| 4:30 | **Billing & audit**, Verify chain | Every hash from the first event to now recomputes. Every claim in the last four minutes is provable. |
| 4:50 | Story, the pipeline | Five squares, five processes, five green heartbeats. "No single process can both decide and act." |

## What is not in the cut, and why

- **Killing a process on camera.** The chaos runner does kill each service and the fleet holds, but
  `GET /og/api/health` still reports a dead process as healthy (known limitation, `api/store.py`
  `health_snapshot`). Fix that one query and this becomes the strongest minute in the video.
- **Typing the spike into the copilot instead of running a scenario.** The copilot is advisory and
  read-only by design; a demo director that turns a sentence into a simulator scenario is the next
  build, and it keeps the copilot's guarantees intact because it drives the simulator, not the fleet.
- **A member's view.** "Homes first" is a constraint today, not a screen. The Story page says so.

## Before you record

1. `og-feeds` on the simulator, or steps at 1:10 and 2:05 do nothing (`dev/config/dev.toml`).
2. Seed at least one committed obligation with a window that starts one gate ahead, or the lock has
   nothing to hold (`docs/demo/README.md`, Topic 3).
3. Open `/og/story` first and let the stream connect; the header pill reads "live".
