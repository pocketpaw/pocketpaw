# schemas — Request/response data validation for the knowledge base REST API

> This module defines Pydantic request/response schemas for the knowledge base domain, providing type-safe contract definitions for REST API endpoints. It exists as a separate module to centralize data validation logic and serve as a single source of truth for API input/output structures across router, service, and messaging layers. These schemas enforce business constraints (query length, result limits, scope overrides) at the API boundary.

**Categories:** knowledge base domain, API layer, data validation, request/response contracts  
**Concepts:** SearchRequest, IngestTextRequest, IngestUrlRequest, LintRequest, Pydantic BaseModel, Field constraints (min_length, ge, le), API contract, Request validation, Workspace scoping, Optional scope override  
**Words:** 1453 | **Version:** 1

---

## Purpose

The `schemas` module is the **API contract layer** for the knowledge base domain. It defines the shape, validation rules, and constraints for all data flowing into and out of knowledge base operations.

**Why it exists:**
- **Single source of truth**: All consumers (REST router, internal services, WebSocket handlers, message processors) reference the same schema definitions, eliminating duplication and ensuring consistency
- **Early validation**: Pydantic validates incoming requests at the API boundary before they reach business logic, catching malformed data immediately
- **Type safety**: Python and IDE tooling can infer types from these schemas, reducing runtime errors
- **Constraint enforcement**: Encodes business rules (minimum query length, result limits) as declarative field constraints rather than scattered validation code
- **API documentation**: Serves as the specification for API consumers (can auto-generate OpenAPI/Swagger docs)

**Role in architecture:**
This module sits at the **HTTP API boundary layer**, immediately below the router. When a request arrives at a FastAPI endpoint, FastAPI uses these schemas to parse and validate the JSON payload. If validation fails, FastAPI returns a 422 Unprocessable Entity before the endpoint handler executes. If it succeeds, the endpoint receives a populated, validated model instance.

## Key Classes and Methods

### SearchRequest
Represents a knowledge base search query.

**Fields:**
- `query: str` — The search text. Must be 1+ characters (enforced by `min_length=1`). This is the primary input; empty queries are rejected at the schema level.
- `scope: str | None` — Optional workspace scope override. If provided, restricts search to that scope; if `None`, the default workspace scope is used. Allows cross-workspace queries when explicitly requested.
- `limit: int` — Result count ceiling. Defaults to 10, constrained to `ge=1, le=100` (must be between 1 and 100 inclusive). This prevents accidental or malicious unbounded result sets that could exhaust memory or timeout.

**Purpose:** Validates search operation input. Used by the search router endpoint to type-check and bound the search request before calling the search service.

### IngestTextRequest
Represents a request to add text content to the knowledge base.

**Fields:**
- `text: str` — The text content to ingest. Must be 1+ characters. Rejects empty payloads.
- `source: str` — Metadata indicating where the text came from. Defaults to `"manual"` (user-entered). Could also be `"api"`, `"upload"`, etc. Enables audit trails and content categorization without requiring it in every request.
- `scope: str | None` — Optional scope override, same as SearchRequest. Allows ingestion into a specific workspace.

**Purpose:** Validates direct text ingestion (e.g., user pastes content into a form, or programmatically pushes text via API). Distinguishes from URL-based ingestion.

### IngestUrlRequest
Represents a request to ingest content from a URL.

**Fields:**
- `url: str` — The URL to fetch and ingest. Must be 1+ characters. No further validation (e.g., no regex URL validation) at the schema level; the service layer is responsible for fetching and validating the URL actually resolves.
- `scope: str | None` — Optional scope override.

**Purpose:** Validates URL-based ingestion requests. Simpler than `IngestTextRequest` because the service must fetch and parse the URL content itself; the schema only validates the input URL string exists.

### LintRequest
Represents a request to lint/validate the knowledge base.

**Fields:**
- `scope: str | None` — Optional scope override. Allows linting a specific workspace or all knowledge base content.

**Purpose:** Triggers knowledge base linting operations (e.g., checking for malformed entries, broken links, consistency violations). Minimal schema because linting is scope-driven and takes no additional parameters in this design.

## How It Works

**Data flow:**

1. **HTTP Request arrives** → FastAPI router receives raw JSON body
2. **Pydantic parsing** → FastAPI automatically instantiates the appropriate schema class (e.g., `SearchRequest`) from the JSON
3. **Validation** → Pydantic runs all Field constraints (min_length, ge, le, etc.). If validation fails, FastAPI returns 422 with validation error details
4. **Type inference** → If validation passes, the router handler receives a fully-typed model instance (e.g., `request: SearchRequest`) with IDE autocompletion
5. **Downstream consumption** → The request model is passed to service layers (SearchService, IngestService, etc.), which can assume the data is already valid

**Key constraints in action:**

- `SearchRequest.query` with `min_length=1`: Prevents searches for empty strings. The service never sees `query=""`.
- `SearchRequest.limit` with `ge=1, le=100`: Prevents requesting 0 results (nonsensical) or 10,000 results (DoS risk). The service always receives `1 <= limit <= 100`.
- `IngestTextRequest.text` with `min_length=1`: Prevents ingesting empty content.
- `scope: str | None`: All request types allow optional scope override. If the client doesn't provide it, the application's default workspace scope is used (logic elsewhere); if provided, it overrides the default. This is optional in the schema but required by business logic at the service/router level.

**Edge cases:**

- **Whitespace-only input**: A string of spaces `" "` passes `min_length=1` validation. Trimming/sanitization is deferred to service logic.
- **Special characters in query**: No regex constraints in the schema; the search engine handles special characters.
- **Large URL strings**: The schema doesn't limit URL length; the HTTP server or reverse proxy may reject overly large payloads before reaching the schema validator.
- **None vs missing**: FastAPI distinguishes between `"scope": null` (explicitly None) and missing `scope` field (uses default None). Both result in `scope=None` at the schema level.

## Authorization and Security

This module **does not implement authorization**. It only validates data structure and format. Authorization ("Can this user access this scope?") is enforced elsewhere—likely in the router layer (via FastAPI dependency injection) or service layer.

**Security considerations:**

- **Input length constraints** (`min_length=1, le=100`) provide basic DoS mitigation by rejecting pathologically large requests.
- **Scope field** allows optional scope override, but no authorization check happens here. The router or service must verify the requesting user has permission to access that scope.
- **Type safety** prevents injection attacks by parsing structured input (JSON) into typed fields rather than string interpolation.

## Dependencies and Integration

**Dependencies (what this module needs):**
- `pydantic.BaseModel, Field` — For schema definition and validation. Pydantic is a mature, widely-used library for this pattern.
- Python 3.10+ type hints (`str | None` syntax) — Requires modern Python.

**Dependents (what uses this module):**

From the import graph, the following modules import from `schemas`:

- **router** — Uses schemas to type-hint endpoint parameters. FastAPI automatically validates incoming JSON against the schemas.
- **service** — May import schemas for type hints on internal function signatures (e.g., `def search(request: SearchRequest) -> SearchResponse`).
- **group_service, message_service** — May use schemas for cross-domain operations (e.g., message_service sends knowledge base queries on behalf of users).
- **ws** (WebSocket handler) — Receives JSON over WebSocket and validates against schemas before passing to service logic.
- **agent_bridge** — An external or autonomous agent interface that constructs and sends knowledge base requests, using schemas to understand the contract.

**Data flow map:**
```
HTTP/WebSocket Client
  ↓ (raw JSON)
router / ws handler
  ↓ (instantiate schema via Pydantic)
SearchRequest | IngestTextRequest | IngestUrlRequest | LintRequest
  ↓ (pass validated model)
service / group_service / message_service / agent_bridge
  ↓ (execute business logic)
Knowledge base operations
```

## Design Decisions

**1. Schema-per-operation pattern**
Rather than a single generic `Request` class, each operation gets its own schema (SearchRequest, IngestTextRequest, etc.). This allows operation-specific constraints:
- Search requires a `query`; ingestion does not.
- Ingestion has a `source` field; search does not.
- Lint has minimal fields.

Trade-off: More classes to maintain, but clearer contracts and better error messages ("LintRequest expects scope, not query").

**2. Optional scope override**
All schemas allow `scope: str | None`. Rather than requiring the client to know the default scope, the client can override it if needed. The application's default is used if not provided.

Trade-off: Slightly more code in services to handle the override logic, but more flexible API for multi-workspace scenarios.

**3. Constrained integers with Field(ge=..., le=...)**
The `limit` field uses Pydantic's `ge` (greater than or equal) and `le` (less than or equal) validators instead of custom validation logic. This is declarative and automatically included in generated API docs.

Trade-off: Constraints are hardcoded (1–100); if you want to vary the limit globally, you'd need to change this file and restart the server.

**4. Minimal validation in schemas**
The schemas validate structure (types, lengths) but not semantics (e.g., "is this URL valid?", "does this scope exist?"). Semantic validation is deferred to service logic. This keeps schemas lightweight and focused on the HTTP API contract.

Trade-off: Service code must still validate; you don't get automatic error responses from schema validation for invalid URLs. But this is appropriate because fetching and validating a URL is a business-logic concern, not a schema concern.

**5. Pydantic BaseModel**
Using Pydantic (rather than dataclasses or hand-rolled validation) provides automatic serialization, JSON schema generation, IDE support, and a massive ecosystem. FastAPI has first-class Pydantic integration.

Trade-off: Adds a dependency; but Pydantic is already ubiquitous in modern Python web frameworks.

---

## Related

- [untitled](untitled.md)
