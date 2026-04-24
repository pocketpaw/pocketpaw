---
{
  "title": "Fabric Journal Store Tests: Scope Isolation, Pagination, Projection Rebuild, and Policy Correctness",
  "summary": "This suite pins four invariants for PocketPaw's journal-backed Fabric object store: correct lifecycle (create/update/archive), scope-based access control that returns empty results rather than leaking existence, post-filter pagination totals, and deterministic projection rebuild from the event journal. It supersedes a previous implementation that leaked object counts across scope boundaries.",
  "concepts": [
    "FabricJournalStore",
    "FabricProjection",
    "scope filter",
    "pagination total",
    "projection rebuild",
    "event journal",
    "Actor attribution",
    "policy decision",
    "glob matching",
    "disaster recovery"
  ],
  "categories": [
    "testing",
    "enterprise edition",
    "journal",
    "access control",
    "test"
  ],
  "source_docs": [
    "e81b892d9c7ac89b"
  ],
  "backlinks": null,
  "word_count": 548,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The Fabric system manages structured business objects (CRM records, project entities, etc.) for enterprise workspaces. It uses an append-only event journal as its source of truth and derives a read model (projection) from that journal. This architecture enables audit trails, point-in-time queries, and disaster recovery by replaying events.

## The Four Invariants This Suite Protects

The file header explicitly names the four invariants:

1. **Happy-path lifecycle** — create → query → update → query → archive → query.
2. **Scope filter** — cross-scope queries return 0 results, not 404, never revealing the object's existence.
3. **Pagination correctness** — `total` is post-filter, never pre-filter.
4. **Projection rebuild** — wipe in-memory state, replay from genesis, reach identical state.

Invariant 3 is described as "the exact leak #938 couldn't close" — a reference to a specific production bug where the total record count was computed before scope filtering, allowing a caller in scope A to infer how many objects existed in scope B by observing that `total > len(results)`.

## Scope Filter: Silent Invisibility

```python
async def test_cross_scope_query_returns_empty_not_error(self, store):
    """A support caller looking at a sales-scoped object sees an
    empty result set — not a 404, not an error, not a count leak."""
```

The design decision to return empty results rather than 404 is deliberate. A 404 would confirm to the caller that the object does not exist in their scope, which is a privacy violation — the caller should not even know the object exists. An empty result set is indistinguishable from "no data exists."

## Projection Rebuild

```python
async def test_rebuild_from_genesis_matches_live_state(journal):
    # Write events, drop projection, rebuild, compare
```

This test creates objects, drops the in-memory projection entirely, replays all events from the journal, and asserts the rebuilt projection matches the live state. This is the disaster recovery guarantee: if the projection store is lost (server crash, deployment rollback), the system can reconstruct it from the journal without data loss.

`TestIncrementalApply` extends this by verifying that appending one new event after a rebuild changes exactly one object's state — confirming that incremental application does not have ordering bugs or double-application issues.

## Scope Required on Write

```python
async def test_create_rejects_empty_scope(store):
    with pytest.raises(...):
        await store.create(_obj(), scope=[])
```

The journal's `EventEntry` invariant requires non-empty scope on every write. An empty scope would make the event visible to all callers (including callers in other organizations), which is a data isolation failure. The store validates scope before writing, rather than relying on the journal layer to catch it.

## Actor Attribution

The suite verifies that custom actors are recorded verbatim and that the default actor is `system:fabric`. This matters for audit trails: every mutation must carry a traceable identity so operators can answer "who changed this record and when."

## Policy Tests (Ported from #938)

`TestPolicyVerbatim` mirrors the policy tests from issue #938, porting them into the journal-based implementation to ensure the decision logic (`visible`, `decide`, `filter_visible`) has the same behavior in the new architecture. The glob-match test (`org:sales:*` matching `org:sales:leads`) is particularly important because it is the primary authorization mechanism for multi-team workspaces.

## Known Gaps

There is no test for concurrent writes from multiple async coroutines. The journal-backed projection is designed to be eventually consistent, but the exact behavior under concurrent append + query has not been verified.