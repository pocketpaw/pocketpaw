---
{
  "title": "Enterprise Cloud Application Bootstrap and Router Mounting",
  "summary": "`ee/cloud/__init__.py` is the application assembly point for the PocketPaw Enterprise REST API. It provides two entry points: `init_realtime()` which wires up the in-process EventBus, and `mount_cloud()` which registers every domain router, the error handler, static file mounts, and lifecycle hooks onto a FastAPI app instance.",
  "concepts": [
    "FastAPI",
    "domain router",
    "EventBus",
    "AudienceResolver",
    "InProcessBus",
    "RedisBus",
    "mount_cloud",
    "init_realtime",
    "ABAC",
    "SSE",
    "agent pool",
    "CloudError",
    "lifecycle hooks"
  ],
  "categories": [
    "architecture",
    "API",
    "realtime",
    "enterprise"
  ],
  "source_docs": [
    "dd9a2b21f01fbe33"
  ],
  "backlinks": null,
  "word_count": 441,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## The Two Entry Points

### `init_realtime()`

The realtime EventBus is a shared singleton that lets domain services publish events (new message, agent action completed, etc.) that are fanned out to connected WebSocket clients. The function builds an `AudienceResolver` — which maps event audience tags like `workspace:abc` to lists of connected user IDs — then creates an `InProcessBus` and registers it as the global bus.

The function is idempotent by design: calling it twice simply overwrites the global with a fresh instance, which is safe during tests that spin up multiple app instances. It is called **synchronously at mount time** rather than in a startup handler because FastAPI's `lifespan` context (used by the host app) suppresses `@app.on_event("startup")` handlers registered after the fact.

The `POCKETPAW_REALTIME_BUS` environment variable is reserved for a future `RedisBus` (tracking Task 33) that would fan events across multiple server processes. Currently only `inprocess` is supported; any other value falls back gracefully with a warning.

### `mount_cloud()`

This function mounts all domain routers under the `/api/v1` prefix in a deliberate order:

```python
auth → workspace → agents → chat → pockets → sessions
→ pockets_journal_stream → kb → knowledge → uploads
→ notifications → files → paw_print
```

The journal stream router for pockets is mounted separately from the main pockets CRUD router. The comment explains why: SSE streams have a different error path and connection lifecycle than regular REST endpoints, and mixing them into the CRUD router would complicate both.

The Files Tab v2 (`/api/v1/files/tree` and `/api/v1/files/browse`) is defined **inline** rather than imported from a module. The comment explains this deliberate choice: the ABAC-based file tree builder needs access to `current_active_user` via `Depends()`, but building that dependency chain from a standalone module without the FastAPI app context requires resolving fastapi-users internals manually. Defining the routes inline avoids that complexity.

## Error Handling

A single `CloudError` exception handler is registered globally. Any domain service that raises `CloudError` (or a subclass) gets serialised to a JSON response with the appropriate HTTP status code. This means individual routers don't need to catch-and-convert these errors themselves.

## Lifecycle Hooks

```python
@app.on_event("startup")  → get_agent_pool().start()
@app.on_event("shutdown") → get_agent_pool().stop()
```

The agent pool is started and stopped with the app. The comment documents a past pitfall: chat persistence was previously handled by a bus subscriber that dual-wrote every turn; that subscriber was removed because `MongoMemoryStore.save()` already handles persistence.

## Known Gaps

- `RedisBus` (multi-process fanout) is not yet implemented. Deployments with more than one worker process will have isolated in-process buses with no cross-process delivery.
- `_NoopKbService` is an inline placeholder that returns empty results for the KB file provider. A real implementation is deferred.