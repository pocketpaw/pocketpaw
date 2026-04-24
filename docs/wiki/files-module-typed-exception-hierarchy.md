---
{
  "title": "Files Module Typed Exception Hierarchy",
  "summary": "Defines the complete typed error hierarchy for PocketPaw's cloud files subsystem. Every exception carries a machine-readable `code` string and a matching HTTP status code so the FastAPI router can translate domain failures into consistent API responses without ad-hoc status mapping.",
  "concepts": [
    "FilesError",
    "ProviderUnsupported",
    "CrossScopeMove",
    "MountReadonly",
    "FilesForbidden",
    "MountNotFound",
    "EntryNotFound",
    "NameConflict",
    "ProviderUpstream",
    "HTTP status mapping",
    "typed exception hierarchy",
    "domain errors"
  ],
  "categories": [
    "files",
    "error-handling",
    "cloud",
    "api"
  ],
  "source_docs": [
    "31fe5612ef430ba0"
  ],
  "backlinks": null,
  "word_count": 610,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `ee.cloud.files.errors` module is the single source of truth for all failure modes that can arise inside PocketPaw's files subsystem. Rather than raising bare Python exceptions or scattering `HTTPException` calls throughout provider and service code, every error condition is represented as a typed subclass of `FilesError`. This keeps domain logic free of FastAPI specifics and lets the router perform a clean, table-driven translation from exception type to HTTP status.

## Base Class: FilesError

`FilesError` extends `Exception` and establishes two class-level attributes that all subclasses inherit or override:

- `code` -- a namespaced string such as `"files.not_found"`. This appears in API error bodies so clients can branch on specific failure types without parsing human-readable messages.
- `http_status` -- defaults to `500`. Subclasses narrow this to the semantically correct HTTP code.

By encoding the HTTP status on the exception class itself, the router catch-all can do a single `except FilesError as exc: raise HTTPException(status_code=exc.http_status, detail=exc.code)` without any isinstance branching.

## Exception Catalog

### ProviderUnsupported (405)
Raised by `BaseFolderProvider` default method stubs when a concrete provider has not overridden a particular operation (e.g., a read-only provider receiving an upload call). The 405 Method Not Allowed status communicates to API clients that the endpoint exists but the specific mount does not support the requested verb.

### CrossScopeMove (409)
Fired when a caller attempts to move a file across provider scope boundaries -- for example, from a personal upload mount into a shared workspace knowledge-base mount. Moves between scopes require a copy-then-delete workflow that the router does not yet implement. The 409 Conflict status signals a logical constraint violation rather than a permission denial.

### MountReadonly (403)
Distinct from `FilesForbidden`, this error targets the mount configuration itself. When a `MountConfig` is marked read-only, mutating operations (upload, rename, delete) raise this error regardless of the user's RBAC permissions. Separating mount-level read-only from user-level forbidden lets the UI display a more informative tooltip: "This folder is read-only" vs. "You don't have permission."

### FilesForbidden (403)
General-purpose access denial used by the ABAC post-filter when a user fails attribute-based checks. The RBAC layer uses `Permission` objects on entries; ABAC uses this error for broader policy denials that span entries.

### MountNotFound (404)
Thrown by `ProviderRegistry.resolve_mount` when no registered mount's path prefix matches the requested path. This is distinct from `EntryNotFound` -- the entire mount is absent, not just a file within it.

### EntryNotFound (404)
Raised by provider `get_entry` and `open_stream` methods when a specific file or folder ID does not exist in the backing store. Providers are responsible for raising this rather than returning `None` so callers can handle absence uniformly.

### NameConflict (409)
Used when an upload or rename would produce a path collision within a mount. Providers that surface this allow the UI to prompt users with a rename-or-replace choice instead of silently overwriting.

### ProviderUpstream (500)
A wrapper for unexpected errors from external storage backends (S3, Google Drive, etc.). By catching backend SDK exceptions and re-raising as `ProviderUpstream`, the aggregator can log the original cause while presenting a stable, non-leaky error shape to clients.

## Design Rationale

The hierarchy mirrors the HTTP status codes precisely because the files API is consumed by a frontend panel that must decide whether to retry, show a permission message, or present a conflict dialog. String codes provide a versioned contract: even if HTTP status codes overlap (two 403s mean different things), the `code` field disambiguates.

## Known Gaps

There is no `ProviderTimeout` or `QuotaExceeded` error class. Upstream timeouts currently surface as `ProviderUpstream`, losing the distinction. Quota violations from cloud providers (e.g., Google Drive storage limits) have no dedicated type and would also collapse into `ProviderUpstream` today.