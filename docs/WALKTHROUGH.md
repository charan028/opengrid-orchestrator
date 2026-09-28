# How this system thinks: a walkthrough for newcomers

Read this before any code. It explains the shape of the system, the rules that produced that shape,
why there are four thousand tests, how the project was built, and what the application actually does
from one second to the next. Every section ends with where to look in the repository.

The one sentence to hold onto: **the orchestrator sells each kilowatt-hour once, to the buyer the
contracts say should have it, proves it was delivered, and measures what that is worth.**

---

## 1. The problem, in plain words

One home battery is a backup product. Thousands of them together are a power plant, and several
kinds of buyer want that plant in the same hour: the homes themselves, the Texas grid operator's
energy and reserve markets, partners buying capacity, utilities deferring upgrades.

The hard part is not selling the energy. It is three things that must all be true at once:

1. **Never sell the same kilowatt twice.** Two buyers cannot both own the next hour of one battery.
2. **Never take a home's outage reserve.** Whatever the price, the energy a home keeps for its own
   blackout is off the table.
3. **Be able to explain every decision afterwards.** To an operator, to a customer, to an auditor.

Everything in the codebase exists to make one of those three true.

Where to look: `README.md`, then `docs/orchestrator/01-product/01-vision-scope-personas.md` section 1.

---

## 2. The architecture: six programs, each doing one job

The style is **separation of duties across independent processes**. No single program can both
decide and act. Each can die without taking the others down.

| Process | Job | Can it act on the fleet? |
|---|---|---|
| `og-feeds` | read market prices, load, wind, solar and weather; validate; store | no |
| `og-engine` | plan with the optimizer; allocate power every two seconds | no, it only proposes |
| `og-guardian` | independently re-check every proposal, then sign it | yes, and only it |
| `og-safestop` | stop a bank, a zone or the fleet | stop only, never release |
| `og-settle` | meter, price, invoice, write the audit chain | no |
| `og-api` | the console, the API, the copilot | no, it relays operator intent |

Two ideas carry the design.

**The one that decides is never the one that acts.** The engine computes what should happen. The
guardian is a separate process holding the only signing key; it re-checks the proposal against the
physical limits and every home's reserve, and signs it. If it vetoes, the command is never published.
Not flagged, not logged as rejected: the code path to publish is unreachable.

**The stop must work when everything else is broken.** Safe stop is its own process with its own key.
The key can only stop. Releasing a stop needs the guardian and two humans.

**How they talk.** PostgreSQL is the single system of record; every state change is a row. MQTT
carries telemetry up from batteries and signed commands down, with every message shape pinned by a
JSON schema in `interfaces/`. There is no workflow engine, no queue framework, no leader election.
Coordination is database locks and version numbers. Fewer moving parts, and every transition is a row
you can query.

**Two products that share no code.** The simulators (`integration-sims/`) pretend to be the batteries,
the grid and the market. They meet the orchestrator only at the wire, so the orchestrator cannot
cheat by knowing how the fake fleet works.

**A console that is mostly HTML.** Server-rendered pages, small partial updates, a live stream for
numbers that change. Almost no client-side state, which is why a real browser can test every screen
against recorded data with no build step.

Where to look: `docs/diagrams/01-system-architecture.svg`,
`docs/orchestrator/02-architecture/01-system-architecture.md`, `interfaces/`.

---

## 3. The rules: fifteen invariants, and the habits around them

The project wrote down fifteen guarantees before writing code, numbered K1 to K15. Each names where
it is enforced and which test proves it. When a piece of code looks strange, the invariant it serves
is usually cited in the docstring.

| Invariant | In one line |
|---|---|
| K1 Homeowner reserve | no command ever takes a home below its outage reserve, at any price |
| K2 One buyer | per battery and interval, each kilowatt-hour backs at most one obligation; the ledger has a single writer |
| K3 Sole signer | nothing moves without the guardian's signature; the battery verifies it |
| K4 Physical envelope | power, current and ramp limits hold for every hub, bank and feeder |
| K6 Command freshness | every command carries a sequence, epoch and lease; stale or replayed ones are dropped |
| K7 Degrade, don't trip | a timeout is not a veto is not a stop; on lost input, hold, then fall back, never step to zero |
| K8 Stop authority | a scoped stop works with the engine down; the stop key cannot release |
| K10 Trace before act | a decision is durably written before it is signed |
| K11 Verifiable record | every event is in a hash chain; anyone can recompute it |
| K13 Commitment lock | a committed obligation is never reduced or reassigned for a better price |

The habits that follow from them:

- **Fail closed, fail fast.** A spent budget refuses in microseconds rather than queuing. A missing
  API key is a normal state the screen names, not an error. Heartbeats are skipped, not faked, when the
  message bus is down, so health sees the truth.
- **Measure, do not assert.** The optimizer's value is proven by running the old rule-based allocator
  alongside it and scoring both with the same evaluator. Prices are never smoothed; an implausible
  one is quarantined and shown as such. Every number on every screen carries its age.
- **Name everything.** Guardian checks are `G-01` to `G-36`, reason codes are `R-*`, alert rules are
  `ALR-*`, UI requirements are `UI-DSP-13` and so on. A log line or a trace row can always be traced
  back to a rule in a document.
- **Write down what is not done.** A known-limitations register lists, bluntly, what is built but
  switched off and what is not built at all. Each lane keeps a file of what it needs from other owners.

Where to look: `docs/orchestrator/07-delivery/00-invariants.md`,
`docs/orchestrator/07-delivery/13-known-limitations.md`, and for the veto made unreachable,
`orchestrator/src/opengrid/guardian/service.py`.

---

## 4. What the application does, second by second

Two clocks run at once. A **fifteen-minute gate** decides what to promise. A **two-second cycle**
decides what to do right now.

```
   every 15 min                                  every 2 s
   ------------                                  ---------
   1 read     feeds poll ERCOT, EIA, weather     4 allocate   each kW to exactly one obligation
   2 forecast P10 / P50 / P90 scenarios          5 trace      write the decision first
   3 select   optimizer picks offers;            6 check+sign guardian runs G-01..G-36, signs
             validator re-derives;              7 dispatch   signed batch over MQTT; hub verifies
             fallback if it fails;              8 telemetry  hubs report back; health, invariants
             chosen offers become obligations
             and lock                            continuously
                                                 9 settle     meter, price, invoice, hash chain
```

1. **Read.** Prices per load zone, load, wind, solar, reserve prices, plus demand and weather. Each
   value is validated on arrival. An implausible one is kept but quarantined; nothing is smoothed.
2. **Forecast.** Three scenarios from recent history, and a firmness check that refuses to underwrite
   a firm promise off a wide band.
3. **Select.** A mixed-integer program chooses which offers to take under those scenarios. An
   independent validator re-derives the constraints from raw numbers; if the plan fails, a greedy
   rule-based allocator takes over. A chosen offer becomes an obligation and walks a state machine:
   offered, selected, committed, delivering, fulfilled or shortfall, settled. Once committed it is
   locked (K13).
4. **Allocate.** Every two seconds, each kilowatt of each bank is given to exactly one obligation, and
   reserved in the ledger (K2).
5. **Trace.** The engine writes the decision's pre-image to the audit chain before proposing (K10).
6. **Check and sign.** The guardian runs its checks and signs with Ed25519 (K3, K4). A veto means the
   batch is never published.
7. **Dispatch.** Commands go over MQTT. Each hub verifies signature, sequence and lease (K6). If
   commands stop, the hub holds its last setpoint until the lease expires, then serves its own home (K7).
8. **Telemetry and health.** Hubs report back. Health rules derive degraded modes, so a stale feed
   stops new commitments rather than crashing anything. Invariant checkers count breaches, which must
   read zero.
9. **Settle.** Meter intervals come back, compliance is scored, revenue, degradation and penalties are
   priced, invoice lines are written, and every step lands in the hash chain (K11).

Around all of it: an operator can always reach the safe stop (K8), and every operator write is a
two-step propose-then-confirm with a countdown and a signed batch.

Where to look: `docs/diagrams/02-dispatch-cycle.svg`, `docs/diagrams/03-commitment-lifecycle.svg`,
`docs/orchestrator/02-architecture/03-decision-engine.md`, then in code:
`orchestrator/src/opengrid/feeds/normalize.py`, `selector/gate.py`, `allocator/cycle.py`,
`ledger/__init__.py`, `guardian/service.py`, `contracts/state_machine.py`, `health/rules.py`,
`trace/store.py`. The Story screen, `ui/routes/story.py`, is a live picture of this section.

---

## 5. Why there are four thousand tests

Three facts about this system make the size of the suite a consequence rather than a choice.

**The promises are safety promises.** "No home below its reserve" cannot be a little bit broken. A
test is the only proof that survives the next change. Every invariant has a property-based test that
generates thousands of random fleets, prices and arrivals and checks the guarantee on all of them.

**Many hands edited in parallel.** People and agents worked in lanes, each owning a set of paths,
with one lead merging around the clock. When you cannot see what everyone else is doing, the suite
is the contract between you.

**The parts were built before the whole existed.** The console was written while the API was a
stub, so its tests run against recorded JSON with the network patched out. The copilot tests block
every outbound socket, so a fake that gets bypassed fails loudly instead of making a real, billed
model call.

| Kind | Roughly | What it proves |
|---|---|---|
| Unit | 3,100 | each module's logic, exact numbers, exact reason codes |
| Simulator unit | 670 | the fake fleet, market and grid behave like the real ones |
| Property-based | 76 | the invariants, under random inputs |
| Integration | 107 | the same logic against a real PostgreSQL |
| End-to-end | 173 | a real browser on every screen; the chaos runner; the performance harness |

Conventions worth copying anywhere:

- A bug found by watching the live system gets a regression test whose comment says "found live".
- The browser suite computes colour contrast from the stylesheet's own tokens and checks that every
  value carries its age, so accessibility and provenance are gated, not reviewed.
- One honest gap: continuous integration runs only the unit, simulator and property suites. The
  integration, end-to-end and performance suites run by hand.

Where to look: `docs/orchestrator/05-testing/01-test-strategy.md`, the traceability matrix beside it,
`orchestrator/tests/property/`, `tests-e2e/ui/test_a11y.py`, `tests-e2e/chaos/`,
`orchestrator/tests/unit/ai_agent/conftest.py` for the network block.

---

## 6. How it was built

**Specification first, with adversaries.** Before code: a brief that pins vocabulary, a decision
register, the invariants, product requirements, architecture, security, the UI specification, a test
strategy with a traceability matrix. Then six adversarial reviews attacked those documents: an
architecture and reliability review, a grid and market review, a red team, a product and judging
review, a claims-verification pass that checked regulatory statements against primary sources, and a
first-principles review. Every finding got a written disposition. Only then were work packages cut.

**Lanes, gates, one merger.** Each work package had an owner and a path set. A pull request had to
pass lint, type checking, a duplication check and the unit suite with coverage, and had to list
anything it needed from other owners' paths rather than touching them. The lead merged into
integration branches, then main, and cut a release tag with a runbook for each.

**AI-assisted, human-led.** The commit history shows it plainly: the largest authors are the build
lead's sessions and named agent accounts working in parallel lanes, with human contributors steering,
reviewing and hot-fixing against the live stack. The lanes, the gates and the invariants are what
made that safe. The known-limitations register is what kept it honest.

Where to look: `BUILD.md`, `docs/orchestrator/00-brief.md`, `docs/orchestrator/00-decision-register.md`,
`docs/orchestrator/06-reviews/`.

---

## 7. A reading order for your first day

1. `README.md`, then open the Story screen (`/og/story`) so the goal is concrete before any code.
2. Section 1 of `docs/orchestrator/01-product/01-vision-scope-personas.md`.
3. `docs/orchestrator/07-delivery/00-invariants.md`. All fifteen. Everything else is downstream.
4. `docs/diagrams/01-system-architecture.svg` and `02-dispatch-cycle.svg`.
5. One process end to end: `orchestrator/src/opengrid/guardian/service.py`, and watch a veto make
   publishing unreachable.
6. `BUILD.md` for how the team worked, then `13-known-limitations.md` for what it knew it had not
   finished.
7. Run the five-minute demo, `docs/demo/FIVE-MINUTES.md`, and inject the price spike yourself.
