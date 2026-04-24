---
{
  "title": "Google Drive Connector Module: Public Surface and Package Exports",
  "summary": "The `pocketpaw.connectors.drive` package is the Google Drive adapter introduced in Workstream C2 of the Org Architecture RFC. Its `__init__.py` defines a minimal public surface, re-exporting the auth helper, HTTP client, error hierarchy, and the `DriveSourceAdapter` that powers zero-copy live federation.",
  "concepts": [
    "connector",
    "Google Drive",
    "SourceAdapter",
    "re-exports",
    "__all__",
    "zero-copy federation",
    "IngestAdapter",
    "DriveClient",
    "DriveSourceAdapter",
    "package public API"
  ],
  "categories": [
    "connectors",
    "Google Drive",
    "module structure"
  ],
  "source_docs": [
    "fc189ab7dbc3f612"
  ],
  "backlinks": null,
  "word_count": 371,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/connectors/drive/__init__.py` is the entry-point module for PocketPaw's Google Drive connector. Its job is straightforward but architecturally significant: it defines the public API of the package by selectively re-exporting symbols from its sub-modules.

## Why a Dedicated `__init__.py`?

Python packages that expose several internal modules (auth, client, errors, source) risk leaking implementation details if callers import directly from sub-modules. Centralising exports here means:

1. **Stable contract** — external code imports `from pocketpaw.connectors.drive import DriveSourceAdapter`, not from `drive.source`. If the internal module name changes the public path stays the same.
2. **Discoverability** — `__all__` is an explicit allowlist, so `from package import *` and IDE auto-completion both surface exactly the symbols intended for external use.
3. **Import hygiene** — because the `__init__` re-exports via relative imports, the sub-modules are only loaded when the top-level package is imported, avoiding circular-import footguns from peer packages.

## Exported Symbols

```python
from .auth import resolve_bearer_token
from .client import DriveClient, DriveFile, DriveRevision
from .errors import (
    DriveAuthError,
    DriveError,
    DriveNotFoundError,
    DriveRateLimitError,
)
from .source import DriveSourceAdapter
```

| Symbol | Purpose |
|---|---|
| `resolve_bearer_token` | Multi-source OAuth token resolution (credential broker → env → OAuthManager) |
| `DriveClient` | Sync HTTP client with exponential-backoff retries against the Drive v3 API |
| `DriveFile` / `DriveRevision` | Normalised dataclasses wrapping raw API shapes |
| `DriveError` hierarchy | Typed errors enabling callers to distinguish auth, rate-limit, and not-found failures |
| `DriveSourceAdapter` | The `SourceAdapter` implementation the retrieval router registers |

## Architecture Context

This module was created as part of **Workstream C2 of the Org Architecture RFC**. The comment explicitly notes two adapter modes:

- **SourceAdapter** (zero-copy live federation) — ships here; the adapter returns `DataRef` pointers that let the router resolve bytes on demand without copying content into PocketPaw's storage.
- **IngestAdapter** (copy-on-ingest) — intentionally deferred until PR #939's ingest primitive settles, to avoid coupling this module to an API that was still changing.

This staging approach reduces integration risk: the live-federation path goes to production first, and the copy path follows once the upstream primitive is stable.

## Known Gaps

- The `IngestAdapter` (copy-on-ingest) for Drive is not yet included. The comment references a follow-up PR once the ingest primitive from PR #939 lands.
