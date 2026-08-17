# Site-build burst harness — what it proves, what it cannot, and how to run it live

Created 2026-08-11 (D5). Companion to `tests/ee/sites/burst_harness.py` and
`tests/ee/sites/test_burst_harness.py`.

Every publish is now a Daytona sandbox. That put the site-build lane's single-flight
guard on the critical path for cost as well as correctness, and until this harness nothing
measured it under contention. This note exists so a green test run is not mistaken for a
validated concurrency cap.

## Running it

```
uv run pytest tests/ee/sites/test_burst_harness.py
```

Under two seconds, no Redis, no Mongo, no Daytona, no network. Every "sandbox" is a
recorded arq enqueue and the clock is injected, so a burst that spans an hour of window
arithmetic finishes in microseconds. If a run of this harness ever asks for
`DAYTONA_API_KEY`, something has been wired wrong — nothing here is allowed to reach the
API.

The mutation plan that proves the assertions bite:

```
uv run python scripts/mutate.py --plan tests/mutations/sites_burst_harness.json
```

## What it found, and what the fix was

**A concurrent burst defeated the single-flight guard.** `enqueue_site_build` gated on a
READ (`should_enqueue`) and then stamped with a WRITE (`mark_build_queued`), awaiting
between the two. Any publish arriving inside that window read a row with no build in
flight, passed the gate correctly, and opened its own sandbox. Measured: **8 concurrent
publishes of one site produced 8 sandboxes.** Two were enough — two clicks inside one round
trip.

The state machine itself was sound, which is what localised the defect:

| Burst shape | Before | After | Reading |
|---|---|---|---|
| Publishes serialised (await each) | 1 | 1 | The rule was never wrong |
| Burst arriving after the stamp landed | 0, all refused | 0, all refused | `should_enqueue` is correct |
| 8 concurrent, one shared row object | 8 | 1 | The read-write window is closed |
| 8 concurrent, each having loaded its own row | 8 | 1 | Closed across workers too |
| 8 concurrent across 8 different sites | 8 | 8 | Correct — the guard is per-site, not global |

The fix (`fix/atomic-queued-stamp`) moves the decision into the write.
`service.claim_build_queued` stamps `queued` through `find_one_and_update` with
`build_state.claim_precondition` as its filter, so the database picks one winner; the losers
write nothing and return `None`, which is the same answer a refused publish gave before.
There is no `should_enqueue` pre-check in front of it any more — one gate, and it is the
conditional write.

**The precondition has to encode staleness, not just status.** A filter of "status is not
queued or building" would refuse a STALE in-flight row, which is the one case the window
deliberately lets through, and that is how a site becomes permanently unpublishable. So it
is a disjunction mirroring `should_enqueue`'s branches: status not in flight, OR the stamp
is not a date (missing, null, or garbage — which reads as stale), OR the stamp is older
than the row's derived window. `TestTheClaimPreconditionMatchesTheGuard` asserts the filter
and `should_enqueue` reach the same verdict on every state, because the rule now lives in
two languages and drift in the strict direction is the expensive one.

A second, smaller observation while sizing anything: `STALE_MARGIN` is 10 minutes, so for
any engine budget well under that the margin dominates the derived window and deriving it
buys little. Both engines currently resolve to 600s, where the two terms are comparable.

## What this harness cannot tell us

Read this before quoting a green run as evidence for a concurrency cap.

- **It does not measure the cap.** No value for the concurrency cap can be derived from
  it. The harness exercises the guard's logic against a fake runner; it has no model of
  how many sandboxes Daytona will actually give us concurrently, or at what point
  throughput stops improving.
- **It does not measure real sandbox contention.** Sandbox creation latency under
  parallel load, Daytona-side queueing, rate limits, and quota rejections are all absent.
  The fake pool always accepts.
- **It does not measure queue latency.** Arq queue depth, worker pickup delay and the
  real distribution of wait time behind a cap are not modelled. The staleness window is
  exercised against an injected clock, which proves the arithmetic, not the wait.
- **It does not measure the deploy race.** The harness counts sandboxes; it does not run
  two artifacts to the deploy step and observe which wins. That the overspend also risks
  a wrong artifact going live is reasoned, not measured.
- **It does not exercise multi-worker or multi-process concurrency.** The separate-doc
  burst is the right shape for it — each publish holds its own snapshot of the row — but
  it runs in one event loop against a fake. A real two-worker race adds Mongo write
  ordering and clock skew.
- **Mongomock is not Mongo.** The one shared-doc burst driven through real Beanie against
  mongomock returned 1 sandbox rather than 8, because mongomock's write resolves without
  yielding to the event loop. Beanie applies its local merge *after* awaiting the write
  (`Document.update` → `merge_models(self, result)`), so against real Mongo that case
  races too. This is why the harness's own `FakeSite.set` yields before applying: it is
  the faithful model, and a fake that applied writes synchronously would report a
  guarantee the lane does not have. The corollary matters for reviewing the fix: a green
  mongomock run proves nothing about atomicity, so the flipped assertions are verified on
  the yielding fake and mongomock is used only where its lack of yielding is irrelevant —
  asserting what the precondition MEANS to a query engine.
- **It does not prove Mongo's atomicity, it assumes it.** `FakeCollection` reproduces the
  one guarantee the fix rests on (a single-document update matches and applies without
  yielding). That guarantee is Mongo's to keep; the harness verifies the lane depends on
  nothing more than it.

Sizing the cap still needs a live run.

## Running it live (deliberate, spends money)

Not automated, and deliberately not wired to a test marker — a live burst creates real
Daytona sandboxes and bills for them, and that is the captain's call, not a side effect of
running a suite. There is no code in the tree that can trigger it.

Preconditions: a staging workspace, `DAYTONA_API_KEY` and `POCKETPAW_REDIS_URL` set for
staging (never production), at least one arq worker running the site-build queue, and an
agreed spend ceiling — at N publishes you are buying N sandboxes.

```bash
# One site, N concurrent publishes. Measures the single-flight guard against real
# infrastructure and prints the number of sandboxes the burst actually opened.
POCKETPAW_ENV=staging \
  uv run python -m pocketpaw_ee.sites.scripts.burst_live \
    --site-id <staging-site-id> --publishes 8 --report /tmp/burst.json
```

`pocketpaw_ee.sites.scripts.burst_live` does not exist yet, on purpose — writing it is
part of whoever runs the live burst, and it should be a thin script that drives the real
publish endpoint N times and then reads back `build_status` / `build_job_id` plus the
Daytona sandbox list for the window. Keeping it unwritten means there is no armed path to
spending money sitting in the repo.

What to record from a live run, since these are the numbers the fake cannot produce:

1. Sandboxes created vs publishes issued (the single-flight result against real writes).
2. Time from publish to `building` at each concurrency level — where queue wait starts
   dominating is where the cap belongs.
3. Any Daytona rate-limit or quota rejection, and which rung classified it.
4. p50/p95 total build time under load vs the measured ~8.7s solo react build. The gap is
   what the cap has to be sized against.
5. Whether any site ended in a terminal status with two artifacts having been produced.
