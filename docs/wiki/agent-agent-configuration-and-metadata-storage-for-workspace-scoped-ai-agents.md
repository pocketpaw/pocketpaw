# agent — Agent configuration and metadata storage for workspace-scoped AI agents

> This module defines the data models for storing agent configurations in the OCEAN system, including both the agent's core metadata (name, workspace, ownership) and its behavioral configuration (model, system prompt, tools, personality traits via the SOUL framework). It exists as a separate model layer to cleanly separate agent *configuration* from agent *execution*, enabling other services to query, update, and orchestrate agents without coupling to runtime concerns. The module is foundational to the agent management system and integrates with higher-level services like AgentService and GroupService that depend on these schemas.

**Categories:** agent management, data model layer, MongoDB document, schema definition, configuration storage  
**Concepts:** Agent, AgentConfig, TimestampedDocument, Beanie ODM, Pydantic BaseModel, workspace scoping, multi-tenancy, LLM backend abstraction, SOUL framework, Big Five personality (OCEAN)  
**Words:** 1617 | **Version:** 1

---

## Purpose

The `agent` module provides the **data model layer** for agent configurations in the OCEAN system. Its core responsibility is to define what an agent *is* (its identity, capabilities, and behavioral settings) separately from what an agent *does* (execution, invocation, state management).

### Why Separate Configuration from Execution?

This separation of concerns is critical because:
- **Agents are long-lived declarative objects**: An agent's configuration is created once and referenced many times across multiple execution contexts, users, and workspaces.
- **Configuration drives behavior without coupling**: Services that invoke agents (group_service, service, agent_bridge) need to query and apply agent config without importing execution logic.
- **Clear ownership and audit trail**: Configuration changes are tracked separately from runtime logs, enabling better governance and debugging.

### Role in System Architecture

This module sits at the **data model layer** and serves as the single source of truth for agent definitions. It is consumed by:
1. **Service layer** (service, group_service) — reads agent config to determine how to invoke agents
2. **Bridge layer** (agent_bridge) — translates agent config into backend-specific execution parameters
3. **API routes** (imported via __init__) — expose agents for CRUD operations via REST

The module depends only on `base` (for TimestampedDocument), keeping its scope tight and reusable.

## Key Classes and Methods

### AgentConfig (Pydantic BaseModel)

**Purpose**: A reusable configuration schema that encapsulates all behavioral parameters for how an agent should operate.

**Key Fields**:

- **Backend Integration**
  - `backend: str = "claude_agent_sdk"` — specifies which LLM backend to use (extensible for future backends like GPT, Llama, etc.)
  - `model: str = ""` — the specific model identifier; empty string means "use backend's default"
  - `system_prompt: str = ""` — the system message sent to the LLM to shape behavior
  - `tools: list[str]` — list of tool/function names the agent can invoke (e.g., ["search", "calculator"])

- **Generation Parameters** (standard LLM hyperparameters)
  - `temperature: float = 0.7` — creativity vs. determinism (0–2 range)
  - `max_tokens: int = 4096` — response length limit
  - `trust_level: int = 3` — custom constraint for permission/capability escalation (1–5 scale)

- **SOUL Framework Integration** (personality and values)
  - `soul_enabled: bool = True` — feature flag for SOUL personality system
  - `soul_persona: str = ""` — a high-level persona description (e.g., "helpful researcher", "strict auditor")
  - `soul_archetype: str = ""` — optional classification into predefined archetypes
  - `soul_values: list[str]` — explicit values the agent should prioritize (default: ["helpfulness", "accuracy"])
  - `soul_ocean: dict[str, float]` — the Big Five personality traits (OCEAN model) scored 0–1
    - `openness`: curiosity and creative thinking
    - `conscientiousness`: attention to detail and reliability
    - `extraversion`: sociability and proactiveness
    - `agreeableness`: cooperation and empathy
    - `neuroticism`: emotional stability (lower is better)

**Design**: AgentConfig is a pure Pydantic BaseModel (not a document), which means it's always embedded in an Agent document and never stored independently. This ensures agent config and agent metadata are always co-located.

### Agent (TimestampedDocument)

**Purpose**: The persistent MongoDB document representing a single agent definition in a workspace. Combines metadata with configuration.

**Key Fields**:

- **Identity & Scope**
  - `workspace: Indexed(str)` — which workspace owns this agent (critical for multi-tenancy)
  - `name: str` — human-readable agent name
  - `slug: str` — URL-friendly unique identifier (typically `workspace:agent-name`)
  - `owner: str` — User ID of the agent creator/owner (for access control)

- **Presentation**
  - `avatar: str = ""` — URL or emoji for UI representation
  - `visibility: str = "private"` — enum: "private" (owner only), "workspace" (all workspace members), or "public" (system-wide)

- **Behavior**
  - `config: AgentConfig = Field(default_factory=AgentConfig)` — embedded configuration object

- **Timestamps** (inherited from TimestampedDocument)
  - `created_at`, `updated_at` — automatic audit trail

**MongoDB Settings**:
```python
class Settings:
    name = "agents"  # collection name
    indexes = [
        [('workspace', 1), ('slug', 1)]  # compound index for efficient scoped queries
    ]
```

This compound index optimizes the common query pattern: *"find agent by workspace and slug"* — enabling fast lookups when resolving agent references in group workflows.

## How It Works

### Data Flow

1. **Creation**: A user creates an agent via an API endpoint, which validates the input against Agent/AgentConfig Pydantic schemas and stores it in MongoDB.
2. **Configuration Retrieval**: When a service (e.g., GroupService) needs to execute an agent, it queries `agents` collection by `(workspace, slug)` using the compound index.
3. **Configuration Application**: The retrieved AgentConfig is passed to agent_bridge, which translates it into backend-specific parameters (e.g., Claude SDK initialization).
4. **Update**: Configuration changes are applied with automatic timestamp updates via TimestampedDocument's middleware.

### Validation & Constraints

- **Trust Level**: Bounded to 1–5 to prevent invalid escalation levels
- **Temperature**: Bounded to 0–2 (standard LLM range)
- **Max Tokens**: Minimum 1 token to prevent empty generations
- **Visibility**: Regex pattern enforces exactly three allowed values
- **Workspace Scoping**: Every agent is bound to a workspace via the indexed field, ensuring isolation in multi-tenant deployments

### Edge Cases

- **Empty model field**: When `model: ""`, the bridge layer interprets this as "use backend's default model" — enabling version-agnostic config
- **Default SOUL values**: If `soul_ocean` is not provided, all OCEAN traits default to sensible middle-ground values (0.7, 0.85, 0.5, 0.8, 0.2)
- **Disabled SOUL**: When `soul_enabled: False`, higher layers should ignore all soul_* fields, treating the agent as a pure LLM without personality constraints

## Authorization and Security

Access control is **not enforced in this module** — it's enforced at the API and service layers:

- **Query Filtering**: Services that fetch agents filter by workspace and visibility before returning config to users
- **Ownership Tracking**: The `owner` field records the creator and can be checked by services to allow owner-only updates
- **Visibility Levels**:
  - `private`: Only the owner can access
  - `workspace`: Any workspace member can access
  - `public`: Any authenticated user can access (system-wide)

The schema itself has no permission logic — it's a pure data container. Permission enforcement happens in service layers (service, group_service) before they query or return Agent documents.

## Dependencies and Integration

### Upstream Dependencies

- **base** (`ee.cloud.models.base`)
  - Provides `TimestampedDocument` — a MongoDB-aware base class with automatic `created_at`/`updated_at` fields
  - Implies the use of Beanie ODM for MongoDB integration

- **Beanie** (`beanie.Indexed`)
  - Indexed wrapper for MongoDB field indexing — the `Indexed(str)` annotation tells Beanie to create a database index on the workspace field

- **Pydantic**
  - BaseModel for schema validation and serialization
  - Field constraints (ge, le, pattern) for runtime validation

### Downstream Dependencies

- **service** — Reads Agent config to expose CRUD operations via REST and coordinates agent execution
- **group_service** — Queries agents by workspace/slug to resolve references in group definitions and orchestrate multi-agent workflows
- **agent_bridge** — Consumes AgentConfig and translates it into backend-specific parameters (e.g., Claude SDK arguments)
- **__init__** — Re-exports Agent and AgentConfig for easy importing across the codebase

### Data Flow Example

```
Client API Request
  ↓
service.create_agent(Agent) ← validates against Agent schema
  ↓
MongoDB agents collection ← stored with timestamps
  ↓
group_service.resolve_agent(workspace, slug)
  ↓
query agents collection using indexed (workspace, slug)
  ↓
agent_bridge.prepare_execution(agent.config)
  ↓
Backend-specific LLM client initialization
```

## Design Decisions

### 1. Configuration as Embedded Document (Not Reference)

**Decision**: AgentConfig is embedded in Agent, not stored separately.

**Rationale**:
- Agent configuration and metadata are always updated together and accessed together
- Avoids extra database lookups
- Ensures configuration consistency — no possibility of a dangling config reference
- Simpler schema semantics: an Agent is self-contained

### 2. SOUL Framework Integration at the Model Layer

**Decision**: Personality and values configuration is stored at the model layer, not hidden in a service or config file.

**Rationale**:
- SOUL traits are part of the agent's persistent identity, not runtime state
- Enables auditing: you can see when and how an agent's persona changed
- Allows different agents in the same workspace to have different personalities
- Separates concerns: the model layer says *what* personality to use; the bridge layer says *how* to apply it

### 3. Workspace Scoping at the Schema Level

**Decision**: Every agent is indexed by workspace.

**Rationale**:
- Multi-tenancy is a first-class concern in OCEAN; scoping it in the schema ensures it can't be accidentally bypassed
- The compound index (workspace, slug) makes the most common query pattern fast
- Prevents accidental cross-workspace access

### 4. Visibility Enum as a String Pattern (Not an Enum Class)

**Decision**: `visibility: str = Field(pattern="^(private|workspace|public)$")` instead of `visibility: VisibilityEnum`

**Rationale**:
- Simpler schema — avoids needing a separate Enum class
- JSON serialization is straightforward (string vs. enum)
- Easier for frontend integration and API documentation
- Pydantic validates the pattern at runtime

### 5. Optional/Empty Model Field

**Decision**: `model: str = ""` (empty string means "use backend default") instead of `model: str | None`

**Rationale**:
- JSON schema compatibility: empty string is cleaner than null for APIs
- Explicit vs. implicit: empty string is a clear "no preference" signal
- Reduces null checks in consuming code

### 6. Trust Level as Custom Constraint

**Decision**: `trust_level: int` (1–5) rather than a backend-native parameter.

**Rationale**:
- OCEAN-specific: trust_level is not a standard LLM parameter; it's a custom permission/capability escalation mechanism
- Allows fine-grained control over what actions an agent can take (e.g., level 5 can delete, level 1 can only read)
- Decoupled from backend: each backend interprets trust_level independently

## Common Patterns in This Module

- **Stateless Document Schema**: Agent and AgentConfig are pure data models with no methods; all business logic lives in service layers
- **Pydantic Validation**: Constraints (ge, le, pattern) ensure invalid configs cannot be persisted
- **MongoDB Indexing**: Compound index on (workspace, slug) optimizes the scoped query pattern
- **Embedding Pattern**: Config is embedded in Agent, not referenced, ensuring atomicity
- **Extensible Backend**: The backend field enables future support for multiple LLM providers
- **Default Factory**: SOUL values use lambda defaults to avoid mutable default issues

---

## Related

- [base-foundational-document-model-with-automatic-timestamp-management-for-mongodb](base-foundational-document-model-with-automatic-timestamp-management-for-mongodb.md)
- [untitled](untitled.md)
- [eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints](eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints.md)
