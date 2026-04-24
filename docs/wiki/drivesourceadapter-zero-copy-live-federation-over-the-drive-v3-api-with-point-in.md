---
{
  "title": "DriveSourceAdapter: Zero-Copy Live Federation over the Drive v3 API with Point-in-Time Query Support",
  "summary": "`DriveSourceAdapter` is the first concrete `SourceAdapter` in PocketPaw, implementing zero-copy live federation against Google Drive: it returns `DataRef` pointers to live files rather than copying bytes into PocketPaw storage. It also pioneered a lightweight `@at=\u003ciso\u003e|query` convention for point-in-time retrieval before soul-protocol adds a native field.",
  "concepts": [
    "SourceAdapter",
    "zero-copy federation",
    "DataRef",
    "point-in-time query",
    "DriveSourceAdapter",
    "RetrievalCandidate",
    "query translation",
    "credential broker",
    "linear score falloff",
    "soul-protocol"
  ],
  "categories": [
    "connectors",
    "Google Drive",
    "retrieval"
  ],
  "source_docs": [
    "8f242155f886bdca"
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

`source.py` implements `DriveSourceAdapter`, which satisfies soul-protocol's `SourceAdapter` protocol and is registered with the retrieval router. Its key characteristic is **zero-copy federation**: instead of pulling file content into PocketPaw, the adapter emits `DataRef` payloads — dicts containing enough identifiers for a downstream resolver to fetch bytes on demand.

## Adapter Contract

The class follows a six-step pattern designed to be replicated for future adapters (Salesforce, Slack, Gmail):

1. Accept a `Credential` from the router's broker.
2. Resolve a bearer token via `resolve_bearer_token`.
3. Build a scoped sync `DriveClient`.
4. Translate the request's `query` into a Drive-native search expression.
5. Return `RetrievalCandidate` rows with `content` shaped as a `DataRef` dict.
6. Return `[]` on empty results — never raise on empty.

## Point-in-Time Query Convention

soul-protocol 0.3.1 does not yet have a structured `point_in_time` field on `RetrievalRequest`. Rather than waiting for the spec bump, `source.py` implements a lightweight convention:

```
@at=2026-04-01T12:00:00Z | Q3 forecast
```

`_split_point_in_time` peels the `@at=` prefix, parses the ISO timestamp, and passes the remainder as the free-text query. When a point-in-time is present, the adapter calls `client.revision_at` to pin the candidate to the matching historical revision. If the revision lookup fails (e.g., Drive has purged old revisions), the adapter logs a warning and falls back to the head revision rather than dropping the candidate entirely.

## Query Translation

```python
def _translate_query(text: str) -> str | None:
```

If the caller passes a Drive-native expression (containing operators like `fullText`, `mimeType`, `owners in`), it is passed through unchanged. Otherwise, the text is wrapped in `fullText contains '...'` with single quotes escaped. This lets power users write Drive query syntax while keeping simple text searches working out of the box.

## Scoring

```python
def _score_for_position(position: int, total: int) -> float:
    # position 0 → 1.0, position total-1 → 0.1
```

Drive returns files sorted by `modifiedTime desc` (or by relevance when `fullText` is used). The linear falloff maps that ordering into `[0.1, 1.0]` scores so the router's multi-source merge respects Drive's intent without swamping projection sources that produce true relevance scores.

## DataRef Payload Shape

```python
{
    "kind": "dataref",
    "source": "drive",
    "id": drive_file.id,
    "name": drive_file.name,
    "mime_type": ...,
    "modified_time": ...,
    "web_view_link": ...,
    "scopes": [...],
    "revision_id": ...,   # optional
    "owners": [...],      # optional
}
```

The `kind="dataref"` marker follows the Zero-Copy convention from the RFC. The router never inspects `RetrievalCandidate.content` — it passes it to the decision-maker unchanged.

## Testability

`client_factory` and `env` are injectable in the constructor:

```python
def __init__(self, *, source_name="drive", client_factory=None, env=None)
```

Tests swap in a stub factory to avoid real HTTP calls without monkeypatching module globals.

## Known Gaps

- `client.revision_at` is called in `source.py` but not defined in the `client.py` public API as reviewed — this implies either a gap in the article for `client.py` or a method added after the AST snapshot.
- soul-protocol's `RetrievalRequest` lacks a native `point_in_time` field; the `@at=` convention is a workaround tracked for a future soul-protocol bump.
