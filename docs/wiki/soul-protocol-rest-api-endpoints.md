---
{
  "title": "Soul Protocol REST API Endpoints",
  "summary": "Exposes the full Soul Protocol interface over REST, covering identity, OCEAN personality state, core memory editing, memory storage and recall, soul import/export, and rubric-based self-evaluation. All endpoints require the `settings:read` scope and safely handle the case where no soul is loaded.",
  "concepts": [
    "Soul Protocol",
    "OCEAN personality",
    "core memory",
    "soul manager",
    "memory recall",
    "soul import",
    "soul export",
    "DID identity",
    "rubric evaluation",
    "soul tiers"
  ],
  "categories": [
    "api",
    "soul-protocol",
    "identity",
    "memory"
  ],
  "source_docs": [
    "7f1491c5dadccd9b"
  ],
  "backlinks": null,
  "word_count": 523,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Soul Protocol REST API Endpoints

The soul router is the REST surface for PocketPaw's persistent AI companion identity system. Where the WebSocket interface handles real-time soul state updates, this router enables structured queries and mutations — useful for dashboards, CLI tools, and external integrations that need a stateless request/response pattern.

### Soul Manager Null Safety

Every endpoint that reads soul state begins with a null check against the soul manager and the loaded soul object. When the soul manager is absent or no soul is loaded, endpoints return a minimal `{"enabled": false}` payload rather than raising an exception. This pattern prevents the entire dashboard from breaking when PocketPaw is running without Soul Protocol configured — a valid deployment mode for users who have not yet set up a soul file.

### Dashboard Endpoint

The `/soul/dashboard` endpoint assembles a comprehensive payload: identity fields (DID, name, birth date, age in days), OCEAN personality scores, current emotional state, recent memories across all tiers, active bonds, and evolution history. The age calculation includes a timezone-awareness guard — soul `born` timestamps may be stored as naive datetimes from older versions, and mixing naive/aware datetimes in Python raises a `TypeError`. The explicit `replace(tzinfo=UTC)` fallback handles this without requiring a migration.

### Core Memory

Core memory is the soul's foundation: a `persona` description (how the AI presents itself) and a `human` description (what the AI knows about its user). GET `/soul/core-memory` returns these two fields. PATCH allows in-place editing with a partial update — only provided keys are changed. This enables targeted persona adjustments without overwriting the human description, and vice versa.

### Memory Operations

- **Remember** (`POST /soul/remember`) stores a new memory with optional importance weighting. The importance score influences how long the memory persists across the evolution and compaction cycles.
- **Recall** (`POST /soul/recall`) performs semantic search over stored memories, returning the most relevant entries up to a limit.
- **Forget** (`POST /soul/forget`) removes memories matching a query — used for privacy-sensitive deletions or when the soul's context window needs to be pruned.

### Soul Import and Export

Export saves the current in-memory soul state back to its `.soul` file on disk. Reload reads the file back, allowing manual edits to the file to take effect without a restart. Import accepts a `.soul`, `.yaml`, `.yml`, or `.json` upload, enabling soul portability across PocketPaw instances. The `import_soul_from_path` endpoint accepts a server-side file path, supporting automated soul migration workflows.

### Self-Evaluation

The `/soul/evaluate` endpoint runs a rubric-based assessment on a response, scoring it against the soul's configured values and personality dimensions. This supports agentic quality-control loops where the soul evaluates its own outputs before surfacing them.

### Known Gaps

The `import_soul_from_path` endpoint accepts an arbitrary filesystem path from the request body. If not validated against an allowlist or jail path, this could allow reading soul files outside the intended soul directory — a path traversal risk similar to the one flagged in PR #622 review. The memory tier listing (`list_soul_memories`) uses a helper `_collect_tier_entries` that truncates results by recency, with no filtering by importance — a low-importance flood of memories could push high-importance ones out of the listing window.