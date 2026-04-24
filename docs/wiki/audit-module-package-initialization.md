---
{
  "title": "Audit Module Package Initialization",
  "summary": "The `pocketpaw.audit` package init re-exports the three public symbols consumers need: `AuditEntry` (the Pydantic model), `AuditStore` (the SQLite backend), and `get_audit_store` (the singleton accessor). This keeps the import path flat for callers while the implementations stay in separate sub-modules.",
  "concepts": [
    "audit package",
    "package init",
    "re-exports",
    "AuditEntry",
    "AuditStore",
    "get_audit_store",
    "enterprise compliance",
    "module boundary",
    "SQLite audit log",
    "public API surface"
  ],
  "categories": [
    "audit",
    "compliance",
    "package-structure",
    "enterprise"
  ],
  "source_docs": [
    "bf8131bc3b1b552c"
  ],
  "backlinks": null,
  "word_count": 323,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Audit Module Package Initialization

The `audit/__init__.py` file serves as the public API surface for PocketPaw's enterprise audit logging subsystem. Its sole purpose is re-exporting the symbols that other modules need to interact with the audit system, without exposing the internal module structure.

### Design Intent

The audit package was created on 2026-03-27 to support government and enterprise compliance requirements. Enterprise customers operating AI agents in regulated environments — finance, healthcare, legal — need an immutable record of every decision the agent made, what data it used, what the AI recommended, and what actually happened. The audit module provides this record.

By consolidating exports in `__init__.py`, the package enforces a clean boundary:

- **`AuditEntry`** — the Pydantic model callers use to construct log entries.
- **`AuditStore`** — the SQLite-backed store for direct instantiation when needed.
- **`get_audit_store`** — the singleton accessor used by the FastAPI dependency injection system.

Callers import from `pocketpaw.audit` rather than from `pocketpaw.audit.models` or `pocketpaw.audit.store`. This means internal reorganizations — splitting the store into separate read/write classes, moving the model to a shared schema package — can happen without changing any caller's import path.

### Package Structure

The audit package contains four files:

| File | Responsibility |
|------|----------------|
| `__init__.py` | Public API re-exports |
| `models.py` | `AuditEntry` Pydantic model |
| `store.py` | `AuditStore` SQLite backend |
| `router.py` | Legacy `/api/v1/audit` endpoint (deprecated) |
| `runtime_router.py` | Canonical `/api/v1/runtime/audit` endpoint |

### Usage Pattern

```python
from pocketpaw.audit import AuditEntry, get_audit_store

store = get_audit_store()
await store.log_entry(
    actor="agent",
    action="approve_action",
    category="decision",
    description="Agent proposed reordering inventory",
)
```

### Known Gaps

The `__all__` declaration is present and correct, but the `router` and `runtime_router` modules are not re-exported. This means callers who want to mount the audit routers in a FastAPI app must import from the sub-module paths directly (`from pocketpaw.audit.router import router`). A consistent pattern would either re-export the routers or document the explicit import path in the module docstring.