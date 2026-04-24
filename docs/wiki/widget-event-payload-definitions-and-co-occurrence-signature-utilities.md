---
{
  "title": "Widget Event Payload Definitions and Co-occurrence Signature Utilities",
  "summary": "Defines the five canonical action names for the widget journal projection domain, their payload builders, and the stable co-occurrence signature algorithm that fixes a token-sort ordering bug shipped in PR #942. All constants are pinned at the module level so the projection, policy, store, and tests share a single import path.",
  "concepts": [
    "action names",
    "widget interaction",
    "widget graduation",
    "co-occurrence detection",
    "token-sort bug",
    "cooccurrence_signature",
    "normalise_signature_tokens",
    "SIGNATURE_MAX_TOKENS",
    "payload builders",
    "accept/dismiss flow",
    "event vocabulary"
  ],
  "categories": [
    "widget-system",
    "event-sourcing",
    "co-occurrence",
    "bug-fix"
  ],
  "source_docs": [
    "ee/widget/events.py"
  ],
  "backlinks": null,
  "word_count": 459,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/widget/events.py` defines what gets written to the org journal for every widget lifecycle event. It is the vocabulary layer of the widget domain: action name constants, payload constructor functions, and the signature utilities that create stable identifiers for co-occurring widget pairs.

## The Five Action Names

```python
ACTION_WIDGET_INTERACTION_RECORDED = "widget.interaction.recorded"
ACTION_WIDGET_GRADUATED            = "widget.graduated"
ACTION_WIDGET_COOCCURRENCE_DETECTED  = "widget.cooccurrence.detected"
ACTION_WIDGET_COOCCURRENCE_ACCEPTED  = "widget.cooccurrence.accepted"
ACTION_WIDGET_COOCCURRENCE_DISMISSED = "widget.cooccurrence.dismissed"
```

The first three were created in Wave 3 / Phase 3 to replace the JSONL approach. The last two were added in Cluster B Sub-PR #2 to support the accept/dismiss flow in the `SuggestedWidgetsFeed` UI (paw-enterprise PR #74).

Having `cooccurrence.accepted` and `cooccurrence.dismissed` as **separate action names** from `widget.interaction.recorded` matters for query efficiency. The projection's GET /widgets/cooccurrence endpoint filters by these specific actions to build the accepted/dismissed sets without walking the full interaction stream, which could be large in production.

## The Co-occurrence Signature Bug and Fix

PR #942 shipped a token-deduplication function with this logic:

```python
# Broken — sorts only the first SIGNATURE_MAX_TOKENS tokens
sorted(tokens[:6])
```

This means two queries with the same 8 words but in different orders would produce different signatures — the pair would be counted as two separate co-occurrences instead of one, defeating deduplication.

The correct version in this module:

```python
def normalise_signature_tokens(text: str) -> list[str]:
    """Lowercase + alnum-tokenise + sort + cap at SIGNATURE_MAX_TOKENS."""
    tokens = sorted(re.findall(r"[a-z0-9]+", text.lower()))
    return tokens[:SIGNATURE_MAX_TOKENS]
```

Sort first, then cap. The `cooccurrence_signature(text_a, text_b)` function builds on `normalise_signature_tokens` to create a stable, canonical string identifier for any pair of queries regardless of the order they are passed in.

Because the projection re-derives signatures from raw widget names on replay, any historical events written by the buggy #942 emitter are corrected automatically during a projection rebuild — the old broken signatures are never stored durably.

## Payload Builders

The module provides one builder per action name:

- `widget_interaction_payload(...)` — encodes widget name, surface, action type, actor, scope context, optional query text
- `widget_graduated_payload(...)` — encodes widget name, surface, old tier, new tier, reason, access counts
- `widget_cooccurrence_payload(...)` — encodes the two widget names, their canonical signature, pair count, and example queries
- `widget_cooccurrence_decision_payload(...)` — shared shape for accepted/dismissed, encoding the signature and operator actor ID

All builders produce plain dicts that are JSON-serializable. The store passes them to `EventEntry` construction.

## Scope Placement Convention

Like `ee/retrieval/events.py`, scope is never included in the payload. Scope lives on `EventEntry.scope` (the journal column). Including it in the payload would create two competing filter surfaces and risk drift.

## Known Gaps

The `SIGNATURE_MAX_TOKENS = 6` cap is a hard-coded constant. Queries with more than 6 meaningful tokens will produce collisions if different long queries share the same top-6 sorted tokens. There is no known plan to make this configurable.