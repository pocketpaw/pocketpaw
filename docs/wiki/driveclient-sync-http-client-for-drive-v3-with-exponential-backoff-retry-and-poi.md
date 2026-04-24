---
{
  "title": "DriveClient: Sync HTTP Client for Drive v3 with Exponential-Backoff Retry and Point-in-Time Awareness",
  "summary": "`DriveClient` is a purpose-built, sync HTTP client for the Google Drive v3 API that handles rate-limit retries with exponential backoff and jitter, normalises file and revision metadata into typed dataclasses, and stays intentionally separate from the global OAuth token store used by PocketPaw's built-in Drive tools.",
  "concepts": [
    "DriveClient",
    "DriveFile",
    "DriveRevision",
    "exponential backoff",
    "rate limit",
    "httpx",
    "Drive v3 API",
    "sync HTTP client",
    "point-in-time",
    "jitter",
    "thread safety"
  ],
  "categories": [
    "connectors",
    "Google Drive",
    "HTTP client"
  ],
  "source_docs": [
    "be9d5f315020669d"
  ],
  "backlinks": null,
  "word_count": 470,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`client.py` provides three types — `DriveFile`, `DriveRevision`, and `DriveClient` — that together form the connector layer's HTTP interface to the Drive v3 API. The client is deliberately sync because soul-protocol 0.3.1 runs `SourceAdapter.query` on a thread pool rather than an async event loop.

## Why a Separate Client?

PocketPaw already has a `pocketpaw.integrations.gdrive.DriveClient` used by the `drive_*` built-in tools. That client is coupled to the global OAuth token store. The connector-layer client exists for a different reason: the retrieval router hands a short-lived `Credential` at dispatch time, so the connector must not reach for any global state. Keeping them separate eliminates coupling and lets each evolve independently.

Using `httpx.Client` instead of `google-api-python-client` avoids pulling in a heavy dependency at runtime. The `google-api-python-client` package is still available as an optional extra for parity with the Gmail/Calendar stacks.

## Data Models

```python
@dataclass
class DriveFile:
    id: str
    name: str
    mime_type: str
    modified_time: str
    size: int | None
    web_view_link: str
    revision_id: str | None
    owners: list[dict[str, Any]]
    raw: dict[str, Any]
```

`DriveFile.from_api` normalises the raw API dict — converting string sizes to `int`, defaulting missing fields to empty strings, and storing the full raw dict for forward-compatibility. `DriveRevision` follows the same pattern for the revision list endpoint.

## Rate Limit Strategy

Google Drive issues quota errors as both HTTP 429 and HTTP 403 with a `userRateLimitExceeded` or `rateLimitExceeded` reason in the JSON body. The helper `_is_rate_limit` handles both:

```python
def _is_rate_limit(resp: httpx.Response) -> bool:
    # checks 429 AND 403 with quota reason string
    ...
```

On a rate-limit response, `_sleep_backoff` applies exponential backoff with random jitter:

- Base: 1.0 s, max cap: 30.0 s, up to 5 retries
- Cap at ~30 s is calibrated to Drive's 100-second per-user quota reset window

Jitter prevents thundering-herd when multiple workers hit the same quota boundary simultaneously.

## Thread Safety and Lifecycle

`DriveClient` is re-entrant but not thread-safe by design. The retrieval router's thread pool creates one client per dispatch, so sharing is not needed. The context manager (`__enter__` / `__exit__`) ensures the underlying `httpx.Client` is closed even when exceptions occur. When the caller injects an existing `httpx.Client` (for testing), `close()` is a no-op on the injected instance — the caller owns its lifecycle.

## Key Methods

| Method | Description |
|---|---|
| `list_files` | Paginated file listing with Drive query expressions |
| `search` | Convenience wrapper: wraps text in `fullText contains '...'` |
| `get_file` | Fetch single file metadata |
| `list_revisions` | Walk the revision history (used for point-in-time queries) |
| `get_content` | Download raw bytes with optional revision pinning and `max_bytes` cap |

## Known Gaps

None flagged in this file, but the parallel `pocketpaw.integrations.gdrive.DriveClient` still exists and the two are not unified. Future cleanup could consolidate once the credential-broker pattern is fully adopted across built-in tools.
