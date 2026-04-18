# ripple_normalizer — Normalizes AI-generated pocket specifications into a consistent, persistence-ready format

> This module provides a single public function, `normalize_ripple_spec()`, that takes potentially incomplete or AI-generated pocket specifications and transforms them into a standardized format with guaranteed envelope fields, valid IDs, and widget metadata. It exists as a dedicated module to centralize the schema validation and enrichment logic that bridges the gap between flexible AI-generated specs and the stricter requirements of the persistence layer. It sits at the boundary between the agent layer (which generates specs) and the service/storage layer (which persists them).

**Categories:** Data Transformation & Normalization, Agent Integration Layer, Specification Management, Utility & Infrastructure  
**Concepts:** normalize_ripple_spec, _short_id, rippleSpec, pocket specification, envelope fields, pure transformation function, format-aware normalization, multi-pane specs, UISpec v1.0, flat widget list  
**Words:** 1412 | **Version:** 1

---

## Purpose

When AI agents or user interactions generate pocket specifications in the OCEAN system, those specs are often incomplete, variable in structure, or missing critical metadata needed for persistence and runtime operation. The `ripple_normalizer` module solves this by providing a lightweight normalizer that:

1. **Ensures structural consistency**: Every spec that passes through gets guaranteed envelope fields (`lifecycle`, `version`, `intent`, `metadata`) regardless of input format.
2. **Generates missing identifiers**: Auto-generates globally unique pocket IDs and widget IDs when not provided, using cryptographically secure random tokens.
3. **Preserves flexibility**: Handles multiple spec formats (multi-pane, UISpec v1.0, flat widget lists) without forcing a single schema.
4. **Enriches metadata**: Applies sensible defaults for color, category, and display configuration.

In the larger system architecture, this normalizer acts as a **data transformation layer** that sits between the agent/generation layer (which produces specs) and the service layer (which persists and retrieves them). It is invoked by `agent_bridge` when specs are generated and by `service` when specs are ingested, ensuring that all specs in the system conform to a predictable structure before they hit the database or are served to the UI.

## Key Classes and Methods

### `_short_id() → str`
**Purpose**: Generate a cryptographically secure random short identifier.

**Implementation**: Uses `secrets.token_hex(4)` to produce an 8-character hexadecimal string. This is a simple, internal utility used whenever a new pocket or widget ID must be generated.

**Why separate?** Keeps ID generation logic isolated and testable; allows future changes to ID format without affecting the main normalization logic.

### `normalize_ripple_spec(spec: dict[str, Any] | None) → dict[str, Any] | None`
**Purpose**: The main entry point. Normalizes a rippleSpec dictionary by ensuring envelope fields, validating structure, and enriching missing metadata.

**Key Business Logic**:

1. **Null/invalid input handling**: Returns `None` if input is `None`, falsy, or not a dictionary. This allows graceful degradation in caller code.

2. **Name extraction**: Tries `spec["title"]` first, falls back to `spec["name"]`. This dual-field approach accommodates both naming conventions in AI-generated specs.

3. **Pocket ID resolution** (in priority order):
   - Use `spec["id"]` if present
   - Fall back to `spec["lifecycle"]["id"]` if present
   - Generate new ID using `pocket-{_short_id()}` format (e.g., `pocket-a1b2c3d4`)

4. **Metadata and color extraction**: Combines color from top level or metadata dict, with fallback to Material Design blue (`#0A84FF`).

5. **Envelope construction**: Builds a consistent envelope dict with:
   - `lifecycle`: Existing value or new `{"type": "persistent", "id": pocket_id}`
   - `title` and `name`: Both set to the resolved name
   - `color`: Resolved color value
   - `metadata`: Merged dict with category (defaulting to `"custom"`), color, and any existing metadata

6. **Format-aware normalization** (three paths):

   **Path A — Multi-pane specs**: If `spec["panes"]` is a dict, the spec is treated as a multi-pane layout. The envelope is merged in and `version` is set to `"1.0"` (or existing value). Everything else passes through unchanged, preserving the complex pane structure.

   **Path B — UISpec v1.0**: If `spec["ui"]` is a dict with a `type` field, it's treated as a structured UISpec. Envelope is merged, `version` defaults to `"1.0"`. The `ui` structure is preserved as-is.

   **Path C — Flat widget list**: If `spec["widgets"]` is a non-empty list, the spec is a simple flat dashboard. This path performs the most transformation:
      - Each widget gets an auto-generated `id` if missing (format: `{pocket_id}-w{index}`, e.g., `pocket-a1b2c3d4-w0`)
      - Each widget gets a `title` from its `name` field or auto-generated `"Widget N"` label
      - `version` defaults to `"2.0"` (indicating flat widget schema)
      - `intent` defaults to `"dashboard"`
      - `display` defaults to `{"columns": 3}`
      - `dashboard_layout` defaults to `{"type": "grid", "columns": 3, "gap": 10}`

   **Path D — No structured content**: If none of the above conditions match, return the spec with just the envelope merged in, preserving whatever structure was provided.

## How It Works

**Data Flow**:

1. **Input**: A dictionary representing a pocket spec, typically from AI generation (`agent_bridge`) or user input (`service`).
2. **Validation**: Check for null/non-dict and bail early if invalid.
3. **Extraction**: Pull all needed fields (name, ID, color, metadata) with cascading fallbacks.
4. **Envelope build**: Assemble the guaranteed minimal set of fields every spec needs.
5. **Format detection & enrichment**: Branch based on structure (panes, ui, widgets, or plain) and apply format-specific transformations.
6. **Return**: A merged spec dict with envelope + format-specific fields.

**Edge Cases Handled**:

- **Null input**: Returns `None` immediately, no error thrown.
- **Empty widgets list**: Treated as no-structure case; returns with envelope only.
- **Widget list with non-dict entries**: Non-dict items are silently skipped; only valid dicts are processed.
- **Missing widget title**: Auto-generated as `"Widget {index + 1}"`.
- **Missing pocket ID across all sources**: A new ID is unconditionally generated.
- **Metadata merge**: Existing metadata is preserved and extended (using `**meta` spread), so custom fields survive normalization.
- **Color priority**: Direct `color` field wins, then metadata color, then hardcoded default. No error if color is invalid CSS; it's passed through as-is for client-side validation.

**Determinism & Idempotence**:
- If a spec is normalized twice and the first result includes auto-generated IDs, the second normalization preserves those IDs (since `spec.get("id")` will now find them).
- ID generation is non-deterministic (uses `secrets.token_hex`), so repeated normalizations of the *same* incomplete spec will generate different IDs—callers must not rely on ID stability until the spec is persisted.

## Authorization and Security

No explicit authorization checks exist in this module. It is a **pure transformation function** with no state, no database access, and no privilege checks. Security is the responsibility of callers:

- **agent_bridge**: Must validate that the AI agent has permission to create specs in the target workspace.
- **service**: Must validate that the user has permission to create or modify pockets before calling this normalizer.

The use of `secrets.token_hex()` (not `random.hex()`) ensures ID generation is cryptographically sound, making IDs unpredictable and suitable as unique identifiers in multi-tenant systems.

## Dependencies and Integration

**External Dependencies**: Only the Python standard library (`secrets` module for cryptographic randomness).

**Internal Dependencies**: None—this module has zero imports from the rest of the codebase, making it a true utility library with no coupling.

**Callers**:
- **agent_bridge**: Invokes `normalize_ripple_spec()` after AI agents generate a spec, before passing it to `service` for persistence.
- **service**: Likely calls this normalizer during spec ingestion to ensure consistency before storing in the database.

**Data Flow**:
```
AI Agent (via agent_bridge)
    ↓
    normalize_ripple_spec()
    ↓
    service (persistence layer)
    ↓
    database / runtime system
```

The normalizer is intentionally placed *before* the service layer to ensure the service always receives a normalized spec, reducing defensive checks downstream.

## Design Decisions

### 1. **Graceful Null Handling**
Returning `None` for invalid input rather than raising an exception allows call sites to decide whether to treat it as an error or a no-op. This is common in data transformation pipelines where invalid input may be expected in some contexts.

### 2. **Format-Aware, Not Format-Enforcing**
The module detects and handles three distinct spec formats (multi-pane, UISpec v1.0, flat widgets) without converting between them. This preserves the semantic richness of complex specs while still normalizing simple ones. A stricter design would force all specs into a single canonical format, but that would lose information and complicate backward compatibility.

### 3. **Minimal Envelope**
The envelope contains only fields essential for persistence and runtime operation: `lifecycle`, `version`, `intent`, `title`, `name`, `color`, `metadata`. Non-essential fields are merged through unchanged (`{**spec, **envelope}`), allowing specs to carry arbitrary extra data without being rejected.

### 4. **Auto-ID Generation with Hierarchical Fallback**
The multi-level ID resolution (direct `id` → `lifecycle.id` → generated) means specs can be built incrementally by different systems without ID collisions, and partial specs can be normalized safely. The fallback to generation ensures IDs never go missing.

### 5. **Widget ID Naming Convention**
Flat widget IDs use the format `{pocket_id}-w{index}` (e.g., `pocket-abc123-w0`), making widget IDs directly traceable to their parent pocket. This enables efficient querying and debugging without requiring a separate parent reference.

### 6. **Version as Format Indicator**
Version `"1.0"` indicates multi-pane or UISpec format (complex, nested); version `"2.0"` indicates flat widget format (simpler, more common). This allows downstream code to branch on version without separate schema detection logic.

### 7. **Secrets Over Random**
Using `secrets.token_hex()` instead of `random` or UUID ensures the IDs are cryptographically unpredictable, important in a system where IDs might be exposed via URLs or APIs and used as access tokens in some contexts.

### 8. **Stateless Pure Function**
The main `normalize_ripple_spec()` function has no side effects, no mutable state, no external I/O. This makes it trivial to test, parallelize, cache, or execute in sandboxed environments. It's a **pure transformation**, not a service.

---

## Related

- [untitled](untitled.md)
