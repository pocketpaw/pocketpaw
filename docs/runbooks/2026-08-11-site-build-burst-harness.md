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

## What it found

**A concurrent burst defeats the single-flight guard.** `enqueue_site_build` gates on a
READ (`should_enqueue`) and then stamps with a WRITE (`mark_build_queued`), and it awaits
between the two. Any publish arriving inside that window reads a row with no build in
flight, passes the gate correctly, and opens its own sandbox. Measured: **8 concurrent
publishes of one site produce 8 sandboxes.** Two are enough to reproduce it — two clicks
inside one round trip.

The state machine itself is sound, which is what localises the defect:

| Burst shape | Sandboxes for one site | Reading |
|---|---|---|
| Publishes serialised (await each) | 1, second returns `None` | The guard works |
| Burst arriving after the stamp landed | 0, all refused | `should_enqueue` is correct |
| 8 concurrent, one shared row object | 8 | The read-write window is open |
| 8 concurrent, each having loaded its own row | 8 | Same window, across workers |
| 8 concurrent across 8 different sites | 8 | Correct — the guard is per-site, not global |

So this is missing atomicity at the row, not a bug in `build_state`. The fix is a
conditional write: stamp `queued` only if the row is still observed non-in-flight
(Mongo `find_one_and_update` with a status precondition), and have the loser of the race
get nothing back and return `None`. When that lands, the assertions in
`TestSingleFlightUnderContention` flip from `sandboxes == BURST` to `sandboxes == 1` and
`refused == BURST - 1`. Those numbers are asserted exactly so that flip is the fix's
acceptance test.

The harness leaves the current behaviour pinned rather than fixed, because the fix is a
change to the write path and belongs in its own reviewed PR.

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
  guarantee the lane does not have.

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
