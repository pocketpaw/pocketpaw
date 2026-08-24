# Vendored conformance fixtures — paw composition kernel

**These files are a copy. They are not the source of truth.**

Source of truth: `paw-workspace/paw-compose/conformance/` (a different repo).
`UPSTREAM-README.md` is that directory's README — the fixture format and harness
contract. `SEMANTICS.md` is a copy of `paw-workspace/paw-compose/SEMANTICS.md`.
Both are vendored so this repo's conformance suite is readable without a second
checkout.

**Pinned at upstream commit `730e593`** — 17 fixtures, spec v0.1.0, copied
2026-08-24. Nothing in this repo verifies that the copy is still current.

## Why vendored

The fixtures live in another repository, and a cross-repo import at test time
would make this repo's test suite depend on a sibling checkout being present
and up to date. Copying keeps `pytest` hermetic.

## Known limitation

Drift is possible and undetected. If `paw-compose/conformance/` changes, this
copy goes stale silently and the suite keeps passing against the old rules. An
automated freshness check — a checksum committed alongside these files and
verified in CI against the upstream directory, or publishing the fixtures as a
versioned artifact both repos consume — is a follow-up, not built here.

To refresh by hand:

```sh
UP=../../../../paw-workspace/paw-compose
rm -f *.json && cp $UP/conformance/*.json .
cp $UP/conformance/README.md UPSTREAM-README.md
cp $UP/SEMANTICS.md .
```

Then update `EXPECTED_FIXTURE_IDS` in `tests/pawkernel/test_conformance.py` so a
fixture that disappears upstream reads as a failure rather than a shrinking
suite, and re-run the mutation checks — an amended fixture is not trustworthy
until a deliberately broken runtime makes it fail.

## Runtime-specific obligations (SEMANTICS.md §7a)

Passing the 17 shared fixtures is not sufficient for conformance. §7a requires
each runtime to cover the hazards the language-neutral fixtures cannot express,
in its own suite, and to record them here. For Python / asyncio:

| Obligation | Covered by |
|---|---|
| A disposer that awaits survives `CancelledError` in the enclosing task; unload is idempotent under repeated cancellation and cleanup is never abandoned partway | `test_async_disposer_is_not_abandoned_when_the_caller_is_cancelled` |
| A disposer error never masks a cancellation, and `CancelledError` is not contained as if it were a failed cleanup step | `test_a_disposer_error_does_not_mask_a_cancelled_caller`, `test_a_cancelled_disposer_is_not_contained_as_a_disposer_error` |
| `parallel` genuinely fans out rather than awaiting listeners in turn | `test_parallel_fans_out_concurrently_and_awaits_every_listener` |

All live in `tests/pawkernel/test_kernel_semantics.py`.

That file also covers three §3 paths the shared suite does not reach. The
`disposer-throws-still-unwinds` fixture exercises `dispose()` to DISPOSED, but
the same teardown is shared by the rollback-to-FAILED path, the
back-to-PENDING path, and child disposal.

## What runs them

`tests/pawkernel/` — `conformance_harness.py` builds the declared plugins and
executes the steps, `test_conformance.py` parametrizes one pytest case per
fixture. The harness fails loudly on any unknown fixture field, step op, or
listener action: a fixture the harness does not understand is a failure, never
a skip.
