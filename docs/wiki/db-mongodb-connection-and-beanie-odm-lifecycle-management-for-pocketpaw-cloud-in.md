# db — MongoDB connection and Beanie ODM lifecycle management for PocketPaw cloud infrastructure

> This module provides a centralized, application-level abstraction for managing MongoDB connections and initializing the Beanie ODM (Object-Document Mapper) in the PocketPaw cloud environment. It exists to decouple database initialization logic from application startup, provide a singleton pattern for the MongoDB client, and ensure consistent document model registration across the cloud system. The module serves as the foundational data persistence layer for all cloud-based features.

**Categories:** data persistence, infrastructure layer, application lifecycle, ODM integration  
**Concepts:** AsyncMongoClient, Beanie ODM, document model registration, singleton pattern, module-scoped state, deferred import, async initialization, connection pooling, graceful shutdown, URI parsing  
**Words:** 1475 | **Version:** 1

---

## Purpose

The `db` module solves a critical architectural problem: **how to reliably initialize and manage MongoDB connectivity in an async Python application while ensuring all document models are registered with the ODM**.

In distributed systems, database initialization must be:
- **Centralized**: A single source of truth for connection configuration prevents inconsistent state
- **Deferred**: Initialization should happen at application startup, not import time, allowing configuration injection
- **Async-aware**: MongoDB operations in PocketPaw are async-first, requiring non-blocking I/O
- **Model-complete**: All Beanie document models must be registered before queries execute, or ODM introspection fails

This module lives at the intersection of three concerns:
1. **Infrastructure layer**: Manages low-level MongoDB/PyMongo connectivity
2. **ODM integration layer**: Bridges MongoDB and Beanie's document model system
3. **Application lifecycle**: Coordinates setup/teardown with application startup/shutdown events

Without this module, every service that needs database access would either duplicate connection logic or import models at module load time (causing circular dependencies and early-bound configuration).

## Key Classes and Methods

### Module-Level State: `_client`

```python
_client: AsyncMongoClient | None = None
```

A module-scoped singleton variable holding the active MongoDB connection. Initialized to `None` and populated by `init_cloud_db()`. This pattern enables lazy initialization and clean shutdown without requiring a class wrapper.

**Why not a class?** The module is stateless except for one resource (the client). A class would add ceremony without benefit. The module acts as a namespace for database operations.

### `async def init_cloud_db(mongo_uri: str)`

**Purpose**: Perform complete database initialization—connect to MongoDB, extract the database name, and register all Beanie document models.

**Key behaviors**:

1. **Global mutation**: Sets the module-scoped `_client` variable. This is intentional—callers can later retrieve the client via `get_client()` without re-initializing.

2. **Connection creation**: 
   ```python
   _client = AsyncMongoClient(mongo_uri)
   ```
   Creates an async MongoDB client. PyMongo's `AsyncMongoClient` defers actual connection until first operation, making this call cheap.

3. **Database name extraction**:
   ```python
   db_name = mongo_uri.rsplit("/", 1)[-1].split("?")[0] or "paw-cloud"
   ```
   Parses the URI to extract the database name. Examples:
   - `mongodb://localhost:27017/paw-cloud` → `paw-cloud`
   - `mongodb://user:pass@cloud.example.com/tenant-db?authSource=admin` → `tenant-db`
   - `mongodb://localhost:27017` → `paw-cloud` (fallback)
   
   This allows environment-specific URIs without hardcoding the database name.

4. **Model registration**:
   ```python
   from ee.cloud.models import ALL_DOCUMENTS
   await init_beanie(database=db, document_models=ALL_DOCUMENTS)
   ```
   Imports all document models from `ee.cloud.models.ALL_DOCUMENTS` and registers them with Beanie. This is a **deferred import**—models are loaded only when database is initialized, avoiding circular imports and ensuring configuration is set before models introspect the environment.

5. **Logging**: Records successful initialization with database name and model count, aiding operational visibility.

**Side effects**: This function must be called exactly once at application startup. Calling it twice will replace the previous client and reinitialize Beanie.

### `async def close_cloud_db()`

**Purpose**: Clean shutdown of the MongoDB connection, enabling graceful app termination.

**Key behaviors**:

1. **Idempotent**: Safely checks if `_client` exists before closing; calling twice is safe.
2. **Connection cleanup**: Closes all pooled connections in the client.
3. **State reset**: Sets `_client = None`, allowing detection of uninitialized state in `get_client()`.

**Typical use**: Registered as a shutdown handler in the FastAPI app's `@app.on_event("shutdown")` or via lifespan context manager.

### `def get_client() -> AsyncMongoClient | None`

**Purpose**: Retrieve the initialized MongoDB client for direct access (e.g., in custom queries or transactions).

**Return value**: The `AsyncMongoClient` if `init_cloud_db()` was called, or `None` if not yet initialized or already closed.

**Design note**: Returns `None` instead of raising an exception, allowing callers to handle uninitialized state gracefully. Consumers should check for `None` before use.

## How It Works

### Initialization Sequence (Typical Application Startup)

```
1. FastAPI app startup event fires
   ↓
2. Application code calls: await init_cloud_db(os.environ["MONGO_URI"])
   ↓
3. AsyncMongoClient created (connection pool initialized, not yet connected)
   ↓
4. Database name extracted from URI
   ↓
5. ALL_DOCUMENTS imported from ee.cloud.models
   ↓
6. Beanie.init_beanie() called → ODM introspects all document classes,
                                    registers indexes, validates schemas
   ↓
7. _client module variable populated
   ↓
8. Logger confirms initialization
   ↓
9. Application handlers (services, routers) can now use get_client()
```

### Data Flow: Query Execution

```
Service code calls Beanie query:
  user = await User.find_one({...})
       ↓
Beanie looks up User in its registry (populated by init_cloud_db)
       ↓
Beanie uses the database connection (passed to init_beanie)
       ↓
Query sent to MongoDB via PyMongo async driver
       ↓
Document returned and deserialized to User instance
```

### Shutdown Sequence

```
1. FastAPI app shutdown event fires
   ↓
2. Application code calls: await close_cloud_db()
   ↓
3. _client.close() terminates all connections
   ↓
4. _client set to None
   ↓
5. Any subsequent get_client() calls return None
```

### Edge Cases

**No initialization**: If code calls `get_client()` before `init_cloud_db()`, it receives `None`. Services using this should either:
- Assume initialization happened (trust application startup)
- Explicitly check and raise an error

**URI parsing edge case**: The URI parser is defensive—malformed URIs fall back to `"paw-cloud"` database name. Example:
- `mongodb://localhost` (no database) → uses `paw-cloud`
- `mongodb://localhost/` (trailing slash) → uses `paw-cloud`

**Multiple initializations**: Calling `init_cloud_db()` twice leaks the first client (old one not closed). This is a bug if it occurs—callers must ensure single initialization.

## Authorization and Security

**No built-in access control**: This module does not enforce authorization. It assumes:
- The calling code is trusted application startup code, not untrusted user input
- The `mongo_uri` is controlled by the application operator (environment variable or config)
- The URI includes authentication credentials if MongoDB requires it

**Security considerations**:
- **Credential handling**: URIs may contain passwords (e.g., `mongodb://user:pass@host`). Ensure URIs are not logged or exposed; the module logs only the database name, not the full URI.
- **URI validation**: The URI is passed directly to `AsyncMongoClient()`, which validates it. Invalid URIs raise exceptions at connection time.
- **Network security**: This module does not configure TLS/SSL; those settings are specified in the URI (e.g., `mongodb+srv://` for MongoDB Atlas).

## Dependencies and Integration

### Dependencies (Incoming)

**External libraries**:
- **`pymongo.AsyncMongoClient`**: Low-level async MongoDB driver. Manages connection pooling, protocol, and raw queries.
- **`beanie.init_beanie`**: ODM initialization. Registers document models, sets up indexing, connects Beanie to the database.
- **Python `logging`**: Standard library; logs initialization messages for operational visibility.

**Internal dependencies**:
- **`ee.cloud.models.ALL_DOCUMENTS`**: A collection of all Beanie document models used in the cloud system. This is a **deferred import**—loaded only at `init_cloud_db()` call time to avoid circular imports.

### Dependents (Who Uses This)

**Inbound calls** (not visible in the import graph, but expected):
- **Application startup code** (likely in `ee/cloud/app.py` or `ee/cloud/main.py`): Calls `init_cloud_db()` and `close_cloud_db()` via FastAPI lifecycle events.
- **Service layer** (e.g., `ee/cloud/services/*.py`): Calls `get_client()` for direct database access when Beanie ORM queries are insufficient (e.g., bulk operations, transactions, aggregation pipelines).
- **Testing/fixtures**: Initializes and tears down the database for test isolation.

### Why Separate from Models

The module imports `ee.cloud.models.ALL_DOCUMENTS` at runtime, not at module load time. This separation prevents circular imports:
- Models may reference services
- Services use this `db` module
- If models imported this module at load time, a cycle would form

The deferred import breaks the cycle: models are loaded only when the app explicitly initializes the database.

## Design Decisions

### Singleton Pattern via Module Variables

**Decision**: Store the client in a module-scoped `_client` variable instead of a class.

**Rationale**:
- Minimizes boilerplate for a single-resource pattern
- Aligns with Python conventions (e.g., `logging.getLogger()` is a module function, not a class method)
- Clean API: `init_cloud_db()`, `get_client()`, `close_cloud_db()` are top-level functions

**Trade-off**: Less testable (global state). Mitigated by ensuring tests call `init_cloud_db()` and `close_cloud_db()` explicitly in setup/teardown.

### Async Initialization

**Decision**: `init_cloud_db()` and `close_cloud_db()` are async functions.

**Rationale**: 
- `init_beanie()` is async (it may perform I/O to introspect the database)
- Aligns with async application startup (FastAPI lifespan events are async)
- Future-proofs: if initialization adds async operations (e.g., schema validation), it's already an async context

**Implication**: Callers must use `await` in async contexts:
```python
@app.on_event("startup")
async def startup():
    await init_cloud_db()
```

### Defensive URI Parsing

**Decision**: Extract database name from URI with a fallback instead of raising an error.

**Rationale**:
- Malformed URIs are typically caught by `AsyncMongoClient()` with clear errors
- Fallback database name (`paw-cloud`) provides a sensible default
- Reduces boilerplate for callers (they don't need to validate the URI format)

**Edge case**: If the URI is intentionally minimal (e.g., `mongodb://localhost`), the module assumes `paw-cloud` as the database, which may not match the actual database name. Operators should use explicit URIs.

### No Client Caching Layer

**Decision**: `get_client()` returns the raw `AsyncMongoClient`, not a wrapper or cache.

**Rationale**:
- `AsyncMongoClient` already manages connection pooling internally
- Callers with specialized needs (e.g., transactions) can access the raw client
- Simpler code path: no indirection

**Trade-off**: Callers are responsible for proper async/await usage; no automatic connection validation.

### Single Database Instance

**Decision**: All document models share one database (extracted from the URI).

**Rationale**:
- Simplifies initialization and shutdown
- Typical for monolithic apps with a single primary database
- Multi-database scenarios would require separate initialization functions

**Future extensibility**: If needed, a sibling function `init_cloud_db_secondary()` could initialize additional databases.