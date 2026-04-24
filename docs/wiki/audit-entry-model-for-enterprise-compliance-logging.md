---
{
  "title": "Audit Entry Model for Enterprise Compliance Logging",
  "summary": "Defines the `AuditEntry` Pydantic model that captures the complete decision context for every auditable action in PocketPaw: who acted, on what data, what the AI recommended, and what actually happened. Field validators enforce closed enum sets for category and status, and serialization helpers bridge between the Pydantic model and SQLite storage.",
  "concepts": [
    "AuditEntry",
    "Pydantic model",
    "field_validator",
    "compliance logging",
    "decision context",
    "audit category",
    "audit status",
    "to_db_row",
    "from_db_row",
    "enterprise audit"
  ],
  "categories": [
    "audit",
    "compliance",
    "models",
    "enterprise"
  ],
  "source_docs": [
    "24954f4ac34c2dd2"
  ],
  "backlinks": null,
  "word_count": 461,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Audit Entry Model for Enterprise Compliance Logging

The `AuditEntry` model is the fundamental record of PocketPaw's compliance audit trail. Every significant agent action — approving a workflow, syncing a connector, changing configuration, flagging a security event — produces one `AuditEntry` that is written to the SQLite audit log and never modified after creation.

### Field Design

The model captures six layers of context:

1. **Identity** — `id` (UUID v4) and `timestamp` (UTC ISO 8601). Both are auto-generated with `default_factory`, ensuring every entry has a unique, time-ordered identifier without requiring the caller to supply one.

2. **Scope** — `pocket_id` links the entry to a specific Pocket (AI agent workspace). Nullable because some entries (system events, config changes) are workspace-global rather than pocket-specific.

3. **Actor and Action** — `actor` identifies who triggered the event (`"agent"`, `"user:prakash"`, `"system"`), and `action` is a machine-readable event name (`"create_pocket"`, `"connector_sync"`). The free-form string design allows new action types without schema migrations.

4. **Category** — one of `"decision"`, `"data"`, `"config"`, `"security"`. This controlled vocabulary enables compliance reports filtered by concern: security auditors focus on `security` and `config`; data governance teams focus on `data`.

5. **Decision context** — `description` (human-readable summary), `context` (raw data dict used to make the decision), `ai_recommendation` (what the model suggested), and `outcome` (what actually happened). Together these four fields reconstruct the full decision chain — essential for post-incident review and for demonstrating to regulators that AI recommendations were reviewed before taking effect.

6. **Status** — `"completed"`, `"approved"`, `"rejected"`, or `"pending"`. The `pending` status supports asynchronous approval workflows where a human must approve an action before the agent executes it.

### Closed Enum Validation

Both `category` and `status` use `@field_validator` with explicit valid-set checks rather than Python `Enum` types. This is a practical choice: Pydantic Enum fields serialize as the enum member name in JSON, which requires all callers to use the same enum class for comparison. String literals with runtime validation give the same enforcement with simpler interop — callers can pass `"decision"` without importing the enum.

### SQLite Serialization

`to_db_row()` converts the model to a flat dict suitable for SQLite insertion. The `context` and `metadata` dict fields are JSON-serialized to strings because SQLite has no native dict column type. `from_db_row()` reverses this, reconstructing a valid `AuditEntry` from a raw database row — handling the JSON deserialization of `context` and `metadata` back to dicts.

### Known Gaps

There is no `version` field on `AuditEntry`. As the model evolves (new fields, changed field meanings), existing rows in the database cannot be distinguished from new rows by version. Adding a `schema_version` field would allow `from_db_row()` to apply migration logic for older entries. The `ai_recommendation` field is a free-form string — structured recommendations (tool call proposals, parameter sets) lose their machine-readability when serialized to plain text.