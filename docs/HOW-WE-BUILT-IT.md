# Why the tests, how we built it, and what the application does: three questions, three stories

This is the short, plain version. `docs/WALKTHROUGH.md` is the full tour. Here each question is
answered by following one real thing through the system, so you can see the reason rather than be
told it.

---

## Question 1: why do we have test cases for this?

**Follow one bug.**

On the second afternoon, someone opened the console against the live stack and every market chart was
empty. Not broken, just blank. The cause was small: the chart asked the API for a product name that
did not exist, so it got nothing and drew nothing. Ten minutes to fix.

Here is the question that decides whether a project has tests: *how would we know if that came back?*

Without a test, the answer is "when someone opens the screen again and notices". With many people
and agents changing code all night, that could be days. So the fix came with a test that loads the
Markets screen against recorded API data and asserts that each chart has at least one line. The
comment on that test says "found live", which is the project's convention for "a real person saw
this break, do not remove". If anyone changes the product name again, the test fails within a minute
of their push, and they find out before the lead does.

That is one kind of test, the regression test. The project has three others, and each answers a
different "how would we know":

| Kind | The question it answers | Example |
|---|---|---|
| Unit | does this function give exactly the right number? | the settlement formula, checked to six decimals |
| Property | does this guarantee hold for inputs nobody thought of? | random fleets and prices, and no home is ever below its reserve |
| Integration | does it still work against a real database? | the ledger refusing a second reservation in PostgreSQL |
| End to end | does a real person, in a real browser, get the right screen? | every page loads, every value shows its age, colours pass contrast |

The property tests are the ones to understand. "No home below its reserve" is a promise to a
customer, not a feature. You cannot check it by trying a few cases. So the test generates thousands
of random situations, fleets, prices, arrivals, failures, and checks the promise on all of them. Every
one of the fifteen invariants has a test like that. They are the project's proof that its promises
are true, and they are the reason the suite is large.

One more reason, practical rather than principled. The console was built before the API existed, so
its tests run against recorded JSON with the network switched off. The copilot's tests go further and
block every outbound socket, so a test that accidentally reached a real model would fail instead of
sending a bill. Tests let parts be built before the whole.

Where to look: `orchestrator/tests/property/` for the invariant tests,
`tests-e2e/ui/test_a11y.py` for the browser checks, `orchestrator/tests/unit/ai_agent/conftest.py`
for the network block.

---

## Question 2: how did we build this?

**Follow one change from idea to main.**

Say the change is the interactive map. Here is its whole life.

1. **It started as a written request**, not a ticket saying "add a map". The request listed what the
   map had to show, why an operator needed it, and which screens it touched. Every request in this
   project reads like that, because the specification documents came first and everything is
   expressed in their vocabulary.
2. **It was assigned to a lane.** Each person owned a set of paths: the console, the docs, the demo,
   the tests. The map lived in the console lane, so the console owner built it. The rule was strict:
   you do not edit the engine, the guardian, the ledger or the allocator from a console branch, even to
   fix a bug you found there. You write the bug down in a file called "needs from other owners" and
   the owner of that path fixes it.
3. **It was built on a branch and had to pass the gate.** Lint, type checking, a duplication check,
   the unit suite with coverage. If any of those failed, the pull request was not looked at. The gate
   ran in continuous integration on every push, so nobody had to ask.
4. **It was reviewed and merged by one person.** The lead merged pull requests into an integration
   branch, ran the wider suites, then merged into main and cut a release tag with a runbook. Nobody
   else pushed to main. That single merger is why a team of people and agents working in parallel
   never stepped on each other.
5. **What it did not fix was written down.** The map needed real coordinates for every battery, and
   the seed data had none. That went into the known-limitations register, bluntly, with the file and
   line, until a separate change in the right lane fixed it.

Behind all of that sat the thing most projects skip: **before any code, the design was attacked.**
Six separate reviews tried to break the written specification, an architecture review, a grid and
market review by domain experts, a red team, a product review, a check of every regulatory claim
against its primary source, and a first-principles review. Each finding got a written answer in a
decision register. The invariants, the fifteen promises, came out of that process. Building code
against promises that had already survived an attack is why so little had to be redesigned later.

And honestly: a large share of the commits were made by AI sessions working in those lanes, with
humans steering, reviewing and fixing against the live stack. The lanes, the gate and the invariants
are what made that safe. The limitations register is what kept it honest.

Where to look: `BUILD.md` for the lane rules and the gate, `docs/orchestrator/06-reviews/` for the
attacks, `docs/orchestrator/00-decision-register.md` for the answers,
`docs/orchestrator/07-delivery/13-known-limitations.md` for what was left undone.

---

## Question 3: what procedure does the application follow?

**Follow one kilowatt-hour.**

It is 15:00 on a hot afternoon. Wholesale prices are climbing. Here is what happens to the energy in
one home battery in Houston over the next hour.

1. **14:45, read.** The feeds process polls the grid operator and stores the price for the Houston
   zone. The value passes validation; had it been absurd, it would have been kept but quarantined,
   never smoothed away.
2. **14:45, forecast.** From recent history, three views of the next hours: pessimistic, expected,
   optimistic.
3. **14:45, select.** A fifteen-minute gate runs the optimizer over every offer on the table under
   those three views. It picks a set. An independent checker re-derives the limits from raw numbers
   and confirms the plan is legal; had it failed, a simple rule-based allocator would have taken
   over. One chosen offer becomes an obligation: 40 kW to a Houston customer from 15:00 to 16:00.
   It is now *committed*, and committed means locked. If the price doubles at 15:20, this obligation
   does not move. That is the promise the counterfactual on the Profitability screen prices.
4. **15:00:00, allocate.** The two-second cycle starts. The allocator gives each kilowatt of each bank
   to exactly one obligation and reserves it in the ledger. Our battery's share of the 40 kW is
   written down, and no other obligation can claim it.
5. **15:00:00, trace.** Before anything is sent, the engine writes the decision to the audit chain.
   If that write failed, nothing would be signed.
6. **15:00:00, check and sign.** A separate process, the guardian, re-checks the batch: ramp limits,
   the bank's current, and above all that our battery stays above the energy the home keeps for its
   own outage. It signs the batch. Had any check failed, the batch would never have been published;
   there is no code path that publishes an unsigned one.
7. **15:00:01, dispatch.** The signed command reaches the battery over the message bus. The battery
   checks the signature, the sequence number and the lease, then discharges. If commands stopped
   arriving, it would hold for a while, then go back to serving its own home.
8. **15:00 to 16:00, watch.** Telemetry flows back every ten seconds. Health rules look for anything
   stale. Invariant checkers count breaches, and the console shows those counts, which must read zero.
9. **16:00, settle.** The meter interval arrives. Delivery is compared with the promise, revenue is
   priced at the zone's real-time price, degradation and any penalty are subtracted, an invoice line
   is written, and every step lands in the hash chain. Later, anyone can recompute that chain from the
   first event and prove none of it was altered.

At every step an operator could have reached the safe stop, which runs on its own process with its
own key. And every action an operator takes is two steps, propose then confirm, with a countdown and
a signed batch, so nothing moves on a single click.

That is the whole procedure. The Story screen in the console is a live picture of it.

Where to look: `docs/diagrams/02-dispatch-cycle.svg`, then in code in this order:
`orchestrator/src/opengrid/feeds/normalize.py`, `selector/gate.py`, `allocator/cycle.py`,
`guardian/service.py`, `trace/store.py`, and `ui/routes/story.py`.
