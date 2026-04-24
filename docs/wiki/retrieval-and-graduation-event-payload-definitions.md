---
{
  "title": "Retrieval and Graduation Event Payload Definitions",
  "summary": "Defines the canonical action names and payload builders for the two journal event types that power PocketPaw's retrieval tracing and memory graduation system. These constants anchor the projection, policy, and store layers to a shared, stable vocabulary sourced from soul-protocol's v0.3.1 action catalog.",
  "concepts": [
    "action names",
    "event payloads",
    "journal events",
    "retrieval.query",
    "graduation.applied",
    "soul-protocol catalog",
    "payload builders",
    "additive schema extension",
    "scope column",
    "event sourcing",
    "memory tiers",
    "ALL_RETRIEVAL_ACTIONS"
  ],
  "categories": [
    "event-sourcing",
    "retrieval",
    "memory-graduation",
    "soul-protocol-integration"
  ],
  "source_docs": [
    "ee/retrieval/events.py"
  ],
  "backlinks": null,
  "word_count": 483,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/retrieval/events.py` is the single source of truth for the two journal events that form the backbone of PocketPaw's retrieval tracing and memory tier-management subsystem:

- `retrieval.query` — emitted every time the retrieval layer queries memory
- `graduation.applied` — emitted every time a graduation policy fires and moves a memory to a new tier

Both action names are pinned as module-level constants (`ACTION_RETRIEVAL_QUERY`, `ACTION_GRADUATION_APPLIED`) and re-exported through `ALL_RETRIEVAL_ACTIONS` so that the projection, policy, store, and tests can all reach them through a single import instead of duplicating string literals.

## Why This Module Exists

String-literal action names scattered across multiple files invite typo-driven mismatches between writers and readers. A single canonical module that all three layers import is the established pattern in PocketPaw's `ee/` domain (the same convention appears in `ee/fabric/events.py`).

Equally important: this module acts as the payload contract between producers and consumers. The `retrieval.query` payload extends soul-protocol v0.3.1's base keys (`request_id`, `query`, `strategy`, `sources_queried`, `sources_failed`, `candidate_count`) with PocketPaw-specific fields (`candidates` with tier + score, `picked` IDs, `pocket_id`, `latency_ms`). This additive design means any downstream reader that only understands the v0.3.1 base shape continues to work — no breaking rename, no version negotiation required.

The `graduation.applied` payload is described as "the first concrete shape" because soul-protocol v0.3.1 lists the action name in its catalog but ships no writer. This module fills that gap by mirroring the `GraduationDecision` structure from PR #937, meaning the projection can fully reconstruct every decision from the event log alone without a secondary data store.

## Scope Placement Convention

A notable design rule enforced in this module's documentation: **scope lives on `EventEntry.scope` (the journal column), never inside the payload**. This prevents drift where two fields claim to represent the same concept but can disagree. The projection filters by the journal column; if the payload also carried scope, an inconsistency would silently corrupt query results. The same rule is enforced in `ee/fabric/events.py`.

## Relationship to the Wider System

This module is a pure-constants + payload-builder file. It has no class definitions and no async logic. Every other module in `ee/retrieval/` imports from it:

- `projection.py` uses the action names to route incoming events to the correct fold logic
- `store.py` uses the payload builders to serialize events before appending them to the journal
- `policy.py` uses `ACTION_GRADUATION_APPLIED` when emitting decisions

The action names themselves come from soul-protocol's versioned catalog, which means PocketPaw's journal is semantically compatible with any other soul-protocol consumer — a cross-process reader that understands `retrieval.query` can parse PocketPaw's events without PocketPaw-specific code.

## Known Gaps

The source notes that `graduation.applied` has "no upstream writer in soul-protocol v0.3.1" — the action name exists in the catalog but no reference implementation ships with the protocol. PocketPaw is the first concrete writer. If soul-protocol later ships its own canonical graduation writer with a different payload shape, this module would need a migration pass to reconcile the two schemas.