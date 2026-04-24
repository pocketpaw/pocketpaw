---
{
  "title": "Integrations Package Initializer — OAuth and Google Workspace Integration Namespace",
  "summary": "The `integrations` package `__init__.py` is a minimal namespace marker created in February 2026 to anchor PocketPaw's OAuth and Google Workspace integration modules (Gmail, Calendar, Drive, Docs). It contains no exports or logic, serving purely as a package boundary.",
  "concepts": [
    "Python package",
    "__init__.py",
    "namespace marker",
    "integrations",
    "OAuth",
    "Google Workspace",
    "package structure",
    "lazy loading",
    "circular imports"
  ],
  "categories": [
    "integrations",
    "package structure"
  ],
  "source_docs": [
    "c828abadd45b1a7b"
  ],
  "backlinks": null,
  "word_count": 319,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/integrations/__init__.py` is the package initializer for PocketPaw's external integrations namespace. It was introduced as part of the Phase 2 Integration Ecosystem in February 2026.

## Contents

The file contains only a module comment:

```python
# Integrations package — OAuth, Gmail, Google Calendar, etc.
# Created: 2026-02-07
```

There are no imports, `__all__` declarations, or re-exports. This is an intentional design choice.

## Why an Empty __init__.py

In Python, a directory becomes an importable package when it contains an `__init__.py` file. The integrations package uses this file purely as a namespace marker, without re-exporting any of its submodules. This pattern keeps the package's public API minimal — consumers must import from specific submodules (`pocketpaw.integrations.gmail`, `pocketpaw.integrations.oauth`, etc.) rather than from the package root.

This approach has several benefits:

- **Explicit imports** — callers know exactly which module they depend on, making refactoring easier.
- **Avoids circular imports** — if `__init__.py` imported from submodules and submodules imported from each other, circular dependency chains would be likely.
- **Lazy loading** — submodule dependencies (httpx, Google API clients) are not imported until the specific client class is actually used.

## Package Scope

The integrations package currently contains:

- `oauth.py` — OAuth 2.0 authorization code flow and token management
- `token_store.py` — Encrypted persistent storage for OAuth tokens
- `gmail.py` — Gmail API client
- `gcalendar.py` — Google Calendar API client
- `gdrive.py` — Google Drive API client
- `gdocs.py` — Google Docs API client

All modules share the `OAuthManager` and `TokenStore` from the `oauth` and `token_store` modules respectively, establishing a common authentication layer across all Google services.

## Known Gaps

- The comment mentions "Gmail, Google Calendar, etc." — the `etc.` could benefit from being expanded as the package grows to include Spotify or other OAuth providers.
- There is no `__all__` defined, which means `from pocketpaw.integrations import *` would import nothing, which is correct behavior but could surprise developers expecting re-exports.
