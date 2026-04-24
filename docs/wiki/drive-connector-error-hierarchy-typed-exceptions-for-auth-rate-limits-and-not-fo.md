---
{
  "title": "Drive Connector Error Hierarchy: Typed Exceptions for Auth, Rate Limits, and Not-Found Failures",
  "summary": "`errors.py` defines a minimal, four-class exception hierarchy for the Drive connector so callers can distinguish auth failures, rate-limit exhaustion, and missing-resource errors without string-sniffing. The deliberate smallness is itself a design decision — each exception maps one-to-one to an actionable recovery path.",
  "concepts": [
    "exception hierarchy",
    "DriveError",
    "DriveAuthError",
    "DriveRateLimitError",
    "DriveNotFoundError",
    "typed exceptions",
    "error handling",
    "retrieval router",
    "sources_failed"
  ],
  "categories": [
    "connectors",
    "Google Drive",
    "error handling"
  ],
  "source_docs": [
    "179d83c065941d50"
  ],
  "backlinks": null,
  "word_count": 330,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`errors.py` is intentionally small. It contains four exception classes and no logic. The comment says it plainly: "Callers should be able to tell 'bad token' from 'rate limited' from 'not found' without string-sniffing."

## The Hierarchy

```python
class DriveError(Exception):          # base
class DriveAuthError(DriveError):     # 401 / missing token
class DriveRateLimitError(DriveError): # 429 / 403 quota after retries
class DriveNotFoundError(DriveError): # 404 / revision missing
```

### Why a Base Class?

`DriveError` lets callers catch all Drive-related failures with a single `except DriveError` clause while still allowing specific handling downstream. The retrieval router catches typed exceptions and records them as `sources_failed` entries — having a base class means the router can safely catch everything from the Drive adapter without also catching unrelated `ValueError` or `IOError` exceptions from other code.

### `DriveAuthError`

Raised when `resolve_bearer_token` finds no usable token, or when `DriveClient` receives an HTTP 401. This maps directly to one recovery path: complete the OAuth flow or set `GOOGLE_OAUTH_TOKEN`.

### `DriveRateLimitError`

Raised only after `DriveClient` exhausts all retries (`max_retries=5` by default). Raising it at this point rather than after the first 429 means transient blips are handled transparently by the backoff logic. A `DriveRateLimitError` reaching the router signals a genuine quota problem that the user or operator must resolve (e.g., wait, reduce concurrency, or upgrade API quota).

### `DriveNotFoundError`

Raised on HTTP 404. Used both for missing files and for revision IDs that no longer exist (Drive can purge old revisions under the `keepForever=False` policy). The caller in `source.py` catches this specifically during point-in-time revision lookups and falls back to emitting the head-revision candidate rather than dropping the result entirely.

## Why Not Just Use HTTPError?

Using `httpx.HTTPStatusError` directly would leak the HTTP layer into the router. The typed hierarchy provides a translation boundary: HTTP status codes are an implementation detail of the Drive API; `DriveAuthError` is a semantic concept the rest of PocketPaw understands.

## Known Gaps

None. The file is intentionally minimal and complete for its scope.
