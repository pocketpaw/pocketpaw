---
{
  "title": "Widget Co-occurrence Accept/Dismiss Endpoint Tests: Route Separation, Validation, and Signature Echo",
  "summary": "Tests the two writer endpoints (`POST /widgets/cooccurrence/accept` and `POST /widgets/cooccurrence/dismiss`) that record user decisions on co-occurring widget suggestions from the SuggestedWidgetsFeed. Pins happy-path response shape, Pydantic validation gates, scope fallback to `[\"org:*\"]`, strict route separation between the two event types, and a signature-echo contract that prevents feed state corruption if the tokenisation rule changes.",
  "concepts": [
    "co-occurrence",
    "SuggestedWidgetsFeed",
    "widget decisions",
    "accept endpoint",
    "dismiss endpoint",
    "route separation",
    "signature echo",
    "scope fallback",
    "Pydantic validation",
    "journal events"
  ],
  "categories": [
    "testing",
    "enterprise features",
    "widget system",
    "user feedback",
    "test"
  ],
  "source_docs": [
    "6e2abcd0ea9a89cf"
  ],
  "backlinks": null,
  "word_count": 488,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/ee/test_widget_cooccurrence_decisions.py` was created in Cluster B Sub-PR #2 (`feat/cluster-b-ripple-journal-stream`) to close the user-feedback loop on paw-enterprise #74's SuggestedWidgetsFeed. The feed surfaces widget pairs that frequently appear together in the same session. Users can accept ("pin this pair") or dismiss ("hide this suggestion"). Both decisions must be journalled so the graduation policy can reflect user intent.

## Why Mirror the Widget Track Style

The file deliberately mirrors the fixture and assertion style of `tests/ee/test_widget_track_endpoint.py`. The intent is that a developer reading both files should perceive them as one story — the track endpoint records raw interactions, and the cooccurrence endpoints record decisions about those interactions. If a future refactor breaks the shared pattern, failures will appear in both suites simultaneously, making the breakage obvious rather than hidden.

## Test Class Breakdown

### TestHappyPath
Both `accept` and `dismiss` return `{ok: true, event_id: "<uuid>"}` on a valid payload. The test reads the journal directly after the POST to assert:
- Exactly one event of the correct action type was written.
- The event actor, scope, signature, widget pair names, and pocket_id all match the request body verbatim.
- For dismiss, the `reason` field propagates into the event payload.

The direct journal read is essential: without it, the endpoint could return a success ack while silently failing to persist the event.

### TestValidation
Parametrised over both routes, tests that missing or empty `signature`, `actor`, and widget name fields all produce 422 before anything touches the journal. The signature field is mandatory because the graduation policy uses it as the canonical deduplication key for a widget pair — a missing signature would produce an untriaged event that the policy cannot classify.

### TestScopeFallback
```python
def test_empty_scope_context_falls_back_to_org_wildcard(client, app):
    res = client.post("/widgets/cooccurrence/accept",
                      json=_valid_payload(actor={"kind": "user", "id": "u", "scope_context": []}))
    ev = journal.query(action=ACTION_WIDGET_COOCCURRENCE_ACCEPTED)[0]
    assert list(ev.scope) == ["org:*"]
```

The journal rejects events with an empty scope list. The UI's anonymous-actor path passes `scope_context=[]`, so the router must substitute `["org:*"]` before appending. This matches the identical fallback in `/widgets/track`.

### TestRouteSeparation
Posts an accept and reads the dismiss stream — verifies zero events appear. Without this test, a router implementation that writes both action types to a shared queue (instead of routing by action string) would pass the happy-path tests while breaking the feed's state machine.

### TestSignatureEcho
The most subtle invariant: the journal event must carry the signature the UI passed in the request, not a recomputed one. If the router recomputed the signature using its current tokenisation rule, and that rule changes between the time the SuggestedWidgetsFeed rendered the suggestion and the time the user clicked accept, the stored signature would not match the one the feed used to display the pair — leaving the feed in a permanently unresolved state for that user.

## Known Gaps

No test covers concurrent accept + dismiss for the same signature (race condition on the policy's decision state). The graduation policy's response to conflicting decisions is untested.