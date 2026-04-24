---
{
  "title": "Legacy Audit REST Router (Deprecated)",
  "summary": "The original `/api/v1/audit` endpoint is kept as a backwards-compatibility alias that forwards to the canonical `runtime_router` implementation. All responses include `Deprecation: true` and a `Link` header pointing callers to the successor path, enabling a graceful migration without breaking existing integrations.",
  "concepts": [
    "deprecated API",
    "deprecation headers",
    "backwards compatibility",
    "audit query",
    "HTTP Deprecation header",
    "Link header",
    "successor-version",
    "AuditStore",
    "legacy alias",
    "migration path"
  ],
  "categories": [
    "audit",
    "api",
    "compliance",
    "deprecation"
  ],
  "source_docs": [
    "06ef00f2eae5f77c"
  ],
  "backlinks": null,
  "word_count": 392,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Legacy Audit REST Router (Deprecated)

The `audit/router.py` file is the first generation of PocketPaw's audit query API. It was introduced on 2026-03-27 and soft-deprecated on 2026-04-19 as part of Cluster C / PR4, which consolidated two separate audit surfaces (`/api/v1/audit` and `/api/v1/instinct/audit`) into the single canonical `/api/v1/runtime/audit` endpoint.

### Why Keep It

Removing `/api/v1/audit` immediately would break existing enterprise integrations that were built against the original path. The deprecation pattern chosen — keeping the endpoint live while signaling its obsolescence — is the standard approach for public APIs with real consumers. The integration has zero migration cost: callers continue to work unchanged, and they receive machine-readable signals telling them to update.

### Deprecation Headers

Every response from the legacy endpoints includes two response headers:

```
Deprecation: true
Link: </api/v1/runtime/audit>; rel="successor-version"
```

The `Deprecation` header follows the IETF draft standard for HTTP API deprecation signals. The `Link` header with `rel="successor-version"` is the machine-readable pointer to the replacement — monitoring tools, API gateways, and sophisticated clients can detect these headers and alert developers automatically. This is more actionable than a comment in documentation.

### Forwarding Strategy

The legacy handler forwards to `store.search_entries()` — the same underlying method used by `runtime_router.py`. It does not call the runtime router function directly, which would introduce an import cycle and complicate testing. Both routers share the same `AuditStore` singleton via the `get_audit_store` dependency, so they read from and write to the same SQLite database.

### Query Parameters

The legacy endpoint accepts `pocket_id`, `category`, `actor`, `date_from`, `date_to`, and `limit` filters — the same set available in the original implementation. The canonical `runtime_router` adds `workspace_id` and `q` (full-text search) on top of these. The legacy endpoint does not add these new parameters, maintaining strict backwards compatibility with clients that may not understand them.

### Export Endpoint

The legacy router also aliases `/api/v1/audit/export` for CSV and JSON exports. This path is similarly forwarded to the store's export methods without modification.

### Known Gaps

The router retains the same `limit` default of 100 as the original implementation, while the canonical router defaults to 200. Callers that migrate from the legacy endpoint to the canonical one without specifying an explicit limit will receive up to twice as many results — a silent behavioral change that could affect clients with fixed-size UIs. A changelog note or aligned defaults would prevent surprises during migration.