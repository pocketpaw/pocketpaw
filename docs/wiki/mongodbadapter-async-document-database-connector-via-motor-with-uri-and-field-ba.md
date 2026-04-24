---
{
  "title": "MongoDBAdapter: Async Document Database Connector via Motor with URI and Field-Based Auth",
  "summary": "`MongoDBAdapter` implements `ConnectorProtocol` using `motor` (async pymongo) to provide full CRUD access to MongoDB collections. It supports both a connection URI and individual host/port/user/password fields, checks for the optional `motor` dependency at connect time, and exposes collection management, document query/insert/update/delete, and aggregation pipeline actions.",
  "concepts": [
    "MongoDBAdapter",
    "motor",
    "pymongo",
    "async database",
    "ConnectorProtocol",
    "URI construction",
    "connection validation",
    "TrustLevel",
    "aggregation pipeline",
    "optional dependency"
  ],
  "categories": [
    "connectors",
    "MongoDB",
    "database"
  ],
  "source_docs": [
    "4ed817a9bd4f3e38"
  ],
  "backlinks": null,
  "word_count": 460,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`MongoDBAdapter` is a native async connector for MongoDB, using the `motor` library (async wrapper around `pymongo`). Unlike the CLI-backed Firebase and GCP adapters, this one makes direct async driver calls, which is appropriate for a database where latency matters and structured queries are the norm.

## Dependency Handling

`motor` is an optional dependency — not everyone using PocketPaw needs MongoDB. The adapter handles this gracefully:

```python
async def connect(self, pocket_id, config):
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
    except ImportError:
        return ConnectionResult(
            success=False,
            message="motor is not installed. Run: uv pip install motor",
        )
```

Deferring the import to connect-time prevents an `ImportError` at module load, which would crash the entire connector registry scan for users who haven't installed motor.

## URI Construction

The adapter accepts credentials in two forms:

1. `MONGO_URI` — a full MongoDB URI (preferred for replica sets, SRV, or TLS options)
2. Individual fields: `MONGO_HOST`, `MONGO_PORT`, `MONGO_USER`, `MONGO_PASSWORD`

When building from fields, user/password are `urllib.parse.quote_plus`-encoded before insertion into the URI. This prevents injection via credentials containing `@`, `/`, or `:` characters.

## Connection Validation

After creating the `AsyncIOMotorClient`, the adapter immediately runs:

```python
await self._client.admin.command("ping")
```

This validates that the server is reachable and the credentials are accepted before returning `CONNECTED`. Without this probe, a misconfigured URI would only fail when the first action runs, producing a confusing error far from the configuration step.

The `serverSelectionTimeoutMS=5000` cap prevents the ping from hanging for the default 30-second motor timeout on unreachable hosts.

## Action Surface

| Action | Description |
|---|---|
| `list_collections` | List all collections in the connected database |
| `find` | Query documents with a MongoDB filter object and limit |
| `find_one` | Retrieve a single document by filter |
| `insert_one` | Insert a document, returns the inserted ID |
| `update_one` | Apply an update operator to a matching document |
| `delete_one` | Delete a single matching document |
| `aggregate` | Run an aggregation pipeline |
| `count_documents` | Count matching documents without fetching them |
| `list_databases` | List all databases on the server |

## Trust Levels

Read operations (`find`, `find_one`, `count_documents`, `list_collections`) are tagged `TrustLevel.AUTO` — the agent can execute them without user confirmation. Write operations (`insert_one`, `update_one`, `delete_one`) are tagged `TrustLevel.CONFIRM`, requiring explicit approval before execution. This prevents unintended data mutation from an LLM hallucinating a write action.

## Known Gaps

- **No `sync()` implementation**: `sync()` is a stub returning `SyncResult(success=False)`. MongoDB data is not pulled into Single Brain.
- **No TLS/SSL configuration**: The adapter builds URIs without TLS options; users needing `tls=true` or client certificates must pass a full `MONGO_URI`.
- **No connection pooling across dispatches**: A new `AsyncIOMotorClient` is created per `connect()` call; connection pool reuse across multiple agent requests is not implemented.
