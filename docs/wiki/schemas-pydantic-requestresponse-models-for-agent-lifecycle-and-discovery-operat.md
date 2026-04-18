# schemas — Pydantic request/response models for agent lifecycle and discovery operations

> This module defines four Pydantic BaseModel classes that serve as the contract layer between HTTP clients and the agent management system. It exists to provide strict input validation, type safety, and clear API documentation for agent creation, updates, discovery queries, and response serialization. By centralizing schema definitions, it ensures consistency across the router, service layer, group operations, messaging, WebSocket handlers, and agent bridge components.

**Categories:** agents domain, API layer, data model, CRUD schema definition  
**Concepts:** CreateAgentRequest, UpdateAgentRequest, DiscoverRequest, AgentResponse, Pydantic BaseModel, Request/Response Schema Pattern, PATCH semantics, Visibility enum (private/workspace/public), OCEAN personality model, soul_archetype, soul_values, soul_ocean  
**Words:** 1503 | **Version:** 1

---

## Purpose

The `schemas` module is the **API contract definition layer** for the agents domain in the PocketPaw system. Its primary purposes are:

1. **Input Validation**: Enforce business rules at the API boundary (e.g., agent names must be 1-100 characters, visibility must be one of three enum values, pagination page must be ≥1).
2. **Type Safety**: Provide Pydantic models that enable mypy/IDE type checking and runtime type coercion.
3. **API Documentation**: Serve as the schema source for OpenAPI/Swagger generation, making the agent API self-documenting.
4. **Cross-layer Contract**: Act as the common language between HTTP handlers (router), business logic (service), real-time handlers (ws), and integrations (agent_bridge, group_service, message_service).

This module exists as separate from service or database layers because schemas represent **client-facing contracts**, not internal domain models. A request schema might differ from a stored entity schema (e.g., UpdateAgentRequest has all-optional fields for PATCH semantics, while the stored Agent entity has required fields).

## Key Classes and Methods

### CreateAgentRequest
**Purpose**: Validates and structures data required to create a new agent.

**Fields**:
- `name` (str, 1-100 chars): Human-readable agent name, required.
- `slug` (str, 1-50 chars): URL-safe identifier, required.
- `avatar` (str): Optional profile image URL or base64 data; defaults to empty string.
- `visibility` (str, enum): Privacy level restricting who can discover the agent. Must be one of: `"private"` (owner only), `"workspace"` (workspace members), `"public"` (all users). Defaults to `"private"`.
- **Agent Config Fields**: `backend`, `model`, `persona` define which LLM backend and model to use. `backend` defaults to `"claude_agent_sdk"`; others default to empty strings, indicating the service should apply workspace or system defaults.
- **Optional Overrides**: `temperature` (float), `max_tokens` (int), `tools` (list[str]), `trust_level` (int), `system_prompt` (str) allow callers to customize inference behavior. All default to None, meaning "use service defaults."
- **Soul Customization Fields**: `soul_enabled`, `soul_archetype`, `soul_values`, `soul_ocean` (dict of personality traits) support the OCEAN personality model. This suggests agents have psychological/personality dimensions beyond just language model configuration.

**Business Logic**: This request represents the minimal required data to instantiate an agent. The presence of soul fields hints that agents are not just prompts + model configs, but have personality representation.

### UpdateAgentRequest
**Purpose**: Validates partial updates to an existing agent (PATCH semantics).

**Key Difference from CreateAgentRequest**: All fields are optional (`| None`). This allows clients to update only the fields they care about.

**Fields**:
- Mirrors CreateAgentRequest's fields but with None defaults.
- Additional `config` (dict) field allows arbitrary backend-specific configuration to be passed through without schema validation, providing extensibility for unforeseen agent config keys.

**Business Logic**: The None defaults mean the router/service must distinguish between "field not provided" (remains None, no update) and "field provided as None/empty" (explicit deletion/clearing). The `config` dict is a **catch-all escape hatch** for agent-specific settings that don't fit the top-level schema.

### DiscoverRequest
**Purpose**: Structures parameters for agent discovery/search queries.

**Fields**:
- `query` (str): Search term; defaults to empty string (may mean "return all" or "match nothing" depending on service implementation).
- `visibility` (str | None): Optional filter to limit results to agents with a specific visibility level. None = no filter.
- `page` (int, ≥1): Pagination cursor; defaults to 1 (first page).
- `page_size` (int, 1-100): Results per page; defaults to 20. Capped at 100 to prevent abuse/large memory allocations.

**Business Logic**: This is a **search/list query model**, not a mutation. The validation constraints (page ≥ 1, page_size ≤ 100) prevent common SQL injection and DOS attack vectors at the API boundary.

### AgentResponse
**Purpose**: Serialization schema for agent entities returned to clients.

**Fields**:
- `id` (str): Unique agent identifier (likely MongoDB ObjectId as string).
- `workspace` (str): Workspace ID; enables multi-tenancy and access control checks.
- `name`, `slug`, `avatar`, `visibility`: Same meaning as in CreateAgentRequest; represent the agent's public-facing properties.
- `config` (dict): The resolved agent configuration (backend, model, persona, temperature, etc.) after service-side defaults have been applied. Returned as a generic dict rather than a structured Pydantic model, suggesting the service handles flattening/nesting.
- `owner` (str): User ID of the agent creator.
- `created_at`, `updated_at` (datetime): Metadata for sorting, caching, and concurrency control. Pydantic automatically parses ISO8601 strings to datetime objects.

**Business Logic**: This is the **output contract**. It includes computed/derived fields (owner, timestamps, resolved config) that requests don't contain, because these are set by the service layer, not the client.

## How It Works

### Request Flow
1. **Client sends HTTP request** with JSON body (e.g., POST /agents with CreateAgentRequest data).
2. **FastAPI/Pydantic deserialization**: The router receives the raw JSON and Pydantic validates it against the schema. If validation fails, a 422 error is returned immediately with field-level error details.
3. **Service layer processes** the validated request object, applying business logic (defaults, access control, LLM calls, database writes).
4. **Response serialization**: The service returns domain objects (e.g., Agent entity from database), which are converted to AgentResponse via Pydantic serialization. The `created_at` and `updated_at` datetimes are automatically ISO8601-encoded.

### Edge Cases & Constraints
- **Empty query in DiscoverRequest**: Behavior depends on service implementation; likely returns all agents the user can see, or returns none. No explicit default behavior in the schema.
- **Optional fields in UpdateAgentRequest**: The service must check for None vs. empty string vs. missing key to avoid accidental deletions (e.g., clearing system_prompt when field was simply omitted).
- **soul_ocean as dict[str, float]**: This is a **flexible key-value structure** allowing arbitrary trait names and scores. The schema doesn't validate trait names or value ranges, enabling extensibility but risking garbage data.
- **visibility pattern validation**: The regex `^(private|workspace|public)$` is enforced at parse time, preventing invalid visibility values from reaching business logic.

## Authorization and Security

This module **enforces no authorization logic itself**; it only validates structure and type. However, it enables authorization downstream:

- **Visibility field**: Guides router/service to enforce access control. A request with `visibility="public"` will be flagged for potential audit/approval if the user is not admin.
- **Workspace scoping**: The AgentResponse includes `workspace` field, allowing API consumers to verify the agent belongs to their workspace before operations.
- **URL-safe slug**: Prevents slug-based agent enumeration or traversal attacks; slugs are constrained to 50 chars and alphanumeric-like patterns (implied, though not explicitly validated in this schema).

Note: No explicit role/permission field in the schemas suggests authorization is handled elsewhere (likely in router via dependency injection, or in service layer).

## Dependencies and Integration

### What This Module Imports
- **pydantic**: BaseModel for validation and serialization.
- **datetime**: For created_at/updated_at timestamps.
- **from __future__ import annotations**: Enables forward references and string-based type hints for cleaner Python 3.7-3.9 compatibility.

### What Depends on This Module (Import Graph)
1. **router**: Deserialization and response serialization in HTTP endpoints (e.g., POST /agents, PATCH /agents/{id}, GET /agents/discover).
2. **service**: Type hints for agent business logic; service methods likely accept CreateAgentRequest/UpdateAgentRequest and return AgentResponse or list[AgentResponse].
3. **group_service**: May accept DiscoverRequest or create DiscoverRequest-like queries to fetch agents for a group.
4. **message_service**: Likely uses AgentResponse to serialize agents referenced in messages or message metadata.
5. **ws** (WebSocket handler): Uses schemas for real-time agent events (creation, update, discovery broadcasts).
6. **agent_bridge**: Integration layer with external agent systems; likely transforms AgentResponse to/from external formats.

### Data Flow Example
```
Client (JSON) 
  → FastAPI Router (deserialize via CreateAgentRequest)
    → Service.create_agent(request: CreateAgentRequest)
      → Database insert (MongoDB, Beanie ODM inferred)
      → Returns Agent entity
    → Router serializes Agent as AgentResponse
  → Client (JSON response)
```

## Design Decisions

### 1. **Separation of Request and Response Schemas**
- CreateAgentRequest and UpdateAgentRequest allow clients to provide input; AgentResponse includes server-computed fields (owner, timestamps, resolved config).
- This prevents clients from forging ownership or timestamps and makes the response contract richer than the request contract.

### 2. **All-Optional UpdateAgentRequest**
- Enables PATCH semantics (partial updates) rather than forcing full-object replacement.
- Downside: Service layer must carefully distinguish None (no update) from empty string (clear field); likely requires explicit null handling logic.

### 3. **Generic dict for config and soul_ocean**
- These fields allow arbitrary key-value data without rigid schema definition.
- **Pro**: Extensible; agents can have bespoke settings without schema migrations.
- **Con**: Runtime type errors; no IDE autocomplete; harder to validate business constraints (e.g., soul_values should not exceed 5 items).

### 4. **Visibility as String Enum Pattern**
- Uses Pydantic `pattern` validation rather than Python Enum, keeping the contract lightweight and JSON-compatible.
- Downside: No type safety on the Python side; developers must string-match or wrap in an Enum themselves.

### 5. **soul_* Fields in Core Schemas**
- The presence of soul customization (archetype, values, ocean) in the core CreateAgentRequest/UpdateAgentRequest suggests agents have **personality-first design**, not just LLM config.
- This hints at a broader system philosophy where agents are treated as autonomous entities with psychological traits, not mere prompt templates.

### 6. **Backend Default to "claude_agent_sdk"**
- Hard-coded default suggests the system primarily targets Claude; other backends are secondary/opt-in.
- Allows backward compatibility: old clients that don't specify backend will still work.

### 7. **Pagination Constraints (page ≥ 1, page_size ≤ 100)**
- Prevents edge case bugs (page 0, negative pages) and DOS attacks (requesting 10M results at once).
- Standard practice in REST APIs.

---

## Related

- [untitled](untitled.md)
