# Vendored conformance fixtures — paw composition kernel

**These files are a copy. They are not the source of truth.**

Source of truth: `paw-workspace/paw-compose/conformance/` (a different repo).
`SEMANTICS.md` here is likewise a copy of `paw-workspace/paw-compose/SEMANTICS.md`,
vendored so this repo's conformance suite is readable without a second checkout.

They were copied on 2026-08-24 at spec version v0.1.0. Nothing in this repo
verifies that the copy is still current.

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
cp ../../../../paw-workspace/paw-compose/conformance/*.json .
cp ../../../../paw-workspace/paw-compose/SEMANTICS.md .
```

## What runs them

`tests/pawkernel/` — `conformance_harness.py` builds the declared plugins and
executes the steps, `test_conformance.py` parametrizes one pytest case per
fixture. The harness fails loudly on any unknown fixture field, step op, or
listener action: a fixture the harness does not understand is a failure, never
a skip.
