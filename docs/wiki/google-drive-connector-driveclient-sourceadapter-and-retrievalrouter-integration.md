---
{
  "title": "Google Drive Connector: DriveClient, SourceAdapter, and RetrievalRouter Integration Tests",
  "summary": "This test suite covers PocketPaw's Google Drive connector end-to-end: HTTP request plumbing with rate-limit backoff, point-in-time revision lookup, `DriveSourceAdapter.query` result shaping, token resolution precedence, and `RetrievalRouter` integration with journal emission. All Drive API traffic is eliminated by a scripted fake `httpx.Client`.",
  "concepts": [
    "DriveClient",
    "DriveSourceAdapter",
    "ScriptedClient",
    "rate limit backoff",
    "point-in-time revision",
    "RetrievalRouter",
    "token resolution",
    "dataref",
    "CandidateSource",
    "journal emission"
  ],
  "categories": [
    "testing",
    "connectors",
    "Google Drive",
    "retrieval",
    "test"
  ],
  "source_docs": [
    "7584850edc969b08"
  ],
  "backlinks": null,
  "word_count": 549,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The Drive connector (`pocketpaw.connectors.drive`) sits at the boundary between PocketPaw's retrieval system and Google's Drive API. It must handle authentication tokens, rate limiting, point-in-time queries, and the `SourceAdapter` protocol expected by the `RetrievalRouter`. This file tests each of those concerns in isolation and then wires them together in a router integration test.

## Scripted HTTP Fake

Rather than pulling in `httpx_mock` or `respx`, the tests use a hand-rolled `ScriptedClient`:

```python
class ScriptedClient:
    def __init__(self, script: list[FakeResponse | Exception]) -> None:
        self._script = list(script)
        self.calls: list[dict] = []

    def request(self, method, url, *, params=None, headers=None, json=None) -> FakeResponse:
        self.calls.append({...})
        nxt = self._script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt
```

The script is consumed in order, one response per call. This design lets tests walk every retry branch precisely — the `test_429_triggers_backoff_and_retries` test scripts two 429 responses followed by a 200, then asserts that exactly two `time.sleep` calls were recorded via monkeypatching.

## Rate-Limit Handling

Google Drive returns 429 for user rate limits and 403 with a `userRateLimitExceeded` reason for quota limits. Both must be retried with exponential backoff:

```python
def test_429_triggers_backoff_and_retries(self, monkeypatch):
    sleep_calls: list[float] = []
    monkeypatch.setattr("pocketpaw.connectors.drive.client.time.sleep",
                        lambda s: sleep_calls.append(s))
    ...
    assert len(sleep_calls) == 2
```

`test_rate_limit_budget_exhausted_raises` verifies that retries are bounded: after `max_retries` attempts all fail with 429, the client raises `DriveRateLimitError` rather than looping forever.

## Point-in-Time Revision Lookup

The Drive API supports file revisions, but querying "what did this file look like at timestamp T" requires fetching all revisions and selecting the most recent one that precedes T:

```python
def test_revision_at_picks_most_recent_before_point(self):
    # Three revisions: Mar 1, Mar 15, Apr 10
    # Point-in-time: Apr 1
    chosen = client.revision_at("file_1", _ts(2026, 4, 1))
    assert chosen.id == "r2"  # Mar 15 — most recent before Apr 1
```

`test_revision_at_requires_aware_timestamp` enforces that callers must pass timezone-aware datetimes. A naive datetime silently compares against UTC revision timestamps using the wrong semantics; the explicit `ValueError` catches this class of bug at the call site.

## DriveSourceAdapter Query Shaping

The adapter wraps `DriveClient.list_files` and converts results into `CandidateSource` objects with `dataref`-kind payloads. Key behaviors under test:

- Free-text queries are translated to `fullText contains 'query'` Drive syntax.
- Native Drive syntax (containing `mimeType`, `name contains`, etc.) passes through unchanged.
- Candidates are ranked by recency (position in the API response).
- When `point_in_time` is present in the request, the adapter calls `revision_at` per file and stamps the correct `revision_id` and `as_of` fields.

## Token Resolution Precedence

`TestResolveBearerToken` pins the resolution order: a credential object passed explicitly wins over an environment variable, which wins over raising `DriveAuthError`:

```python
def test_credential_wins_over_env(self):
    token = resolve_bearer_token(credential=cred, env={"GOOGLE_OAUTH_TOKEN": "env-token"})
    assert token == "cred-token"
```

This hierarchy matters because agents may have per-user OAuth tokens (credential), while a fallback service account token lives in the environment.

## Router Integration with Journal Emission

`TestRouterIntegration` wires a real `RetrievalRouter` with an in-memory credential broker and a soul-protocol journal. It asserts that a successful query emits a `retrieval.query` journal entry with the correct payload fields, and that an auth failure records a `sources_failed` outcome rather than propagating the exception to the caller.

## Known Gaps

Pagination across multiple Drive API pages is not tested — `list_files` currently fetches only a single page. The `test_list_files_happy_path` test scripts one response with one file; multi-page traversal would require chaining `nextPageToken` responses through the `ScriptedClient`.