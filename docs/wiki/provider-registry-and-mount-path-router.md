---
{
  "title": "Provider Registry and Mount Path Router",
  "summary": "Implements `ProviderRegistry`, which maps mount path prefixes to registered `FolderProvider` instances and resolves incoming file paths to the correct provider via longest-prefix matching. The `FolderProvider` Protocol definition here is the canonical duck-typed interface contract all providers must satisfy.",
  "concepts": [
    "ProviderRegistry",
    "FolderProvider",
    "Protocol",
    "longest-prefix match",
    "resolve_mount",
    "MountConfig",
    "ResolvedMount",
    "register",
    "duck typing",
    "mount routing",
    "bootstrap",
    "provider_id"
  ],
  "categories": [
    "files",
    "registry",
    "routing",
    "cloud"
  ],
  "source_docs": [
    "e165e50d95c66149"
  ],
  "backlinks": null,
  "word_count": 466,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee.cloud.files.registry` is the central routing layer of the files subsystem. It answers two questions for every incoming file request: "which provider owns this path?" and "which `MountConfig` applies here?". Without the registry, every router endpoint would need to contain provider selection logic, coupling the HTTP layer to provider internals.

## FolderProvider Protocol

The `FolderProvider` Protocol is defined here rather than in `providers/base.py` to establish a clear architectural boundary. The registry depends on the Protocol; providers depend on neither the registry nor each other. This prevents circular imports and allows providers to be registered by external plugins that never import `registry.py`.

The Protocol is duck-typed: any class that implements the required async methods with matching signatures satisfies it, regardless of inheritance. This matters for testing -- a `MagicMock` with the right attributes is a valid `FolderProvider` without needing to subclass anything.

## ProviderRegistry

### Registration

`register(provider)` adds a provider to the internal list. Providers are registered in the order `bootstrap.py` calls `register`, which typically matches the `order` field in `mounts.yaml`. The registry does not enforce uniqueness on provider IDs -- registering the same provider twice would cause duplicate results in `all()`. Bootstrap is responsible for calling `register` exactly once per provider.

### Lookup by ID

`get(provider_id)` returns the provider whose `provider_id` attribute matches. This is used when the router needs to dispatch a mutation (upload, rename, delete) to a specific provider after the initial path resolution identified which provider owns the target entry.

### Mount Resolution: Longest-Prefix Match

```
resolve_mount(*, path, variables) -> ResolvedMount
```

This is the registry's most critical method. Given a request path like `/workspaces/abc123/my-files/docs/report.pdf` and a `variables` dict containing `{"workspace_id": "abc123", "user_id": "u9"}`, it:

1. Iterates over all `MountConfig` objects in `order` sequence.
2. Calls `resolve_template` on each config's path template to produce a concrete prefix.
3. Selects the config whose resolved prefix is the longest string that is a prefix of the request path.
4. Returns a `ResolvedMount` combining the selected config and provider.

Longest-prefix matching ensures that nested mounts work correctly. If `/workspaces/{workspace_id}` and `/workspaces/{workspace_id}/my-files` are both registered, a path like `/workspaces/abc/my-files/x` routes to the more specific mount.

If no mount matches, `MountNotFound` is raised, producing a 404 response.

## Why Duck-Typed Protocol Rather Than ABC?

An `ABC` would require providers to inherit from it, creating a dependency from every provider back to `registry.py`. With a Protocol, providers only need to implement the correct interface. Third-party or plugin providers can satisfy the Protocol without any import from the `ee.cloud` namespace.

## Known Gaps

- **No provider ID uniqueness enforcement.** Two providers with the same ID can be registered without error, causing `get()` to return the first match only.
- **No dynamic mount registration.** Mounts are loaded at startup and cannot be added or removed at runtime without a restart.