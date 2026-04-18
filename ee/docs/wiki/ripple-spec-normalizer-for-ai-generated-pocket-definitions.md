---
{
  "title": "Ripple Spec Normalizer for AI-Generated Pocket Definitions",
  "summary": "Normalizes AI-generated rippleSpec dictionaries before they are persisted, ensuring consistent envelope fields (version, lifecycle, title, color, metadata) and generating widget IDs when missing. This module acts as a defensive layer between unpredictable LLM output and the storage layer.",
  "concepts": [
    "ripple spec",
    "normalizer",
    "widget ID generation",
    "envelope fields",
    "AI output normalization",
    "pocket definition",
    "defensive parsing"
  ],
  "categories": [
    "cloud",
    "data normalization",
    "AI integration",
    "pockets"
  ],
  "source_docs": [
    "e006108efb8aa0a0"
  ],
  "backlinks": null,
  "word_count": 418,
  "compiled_at": "2026-04-08T07:25:31Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Ripple Spec Normalizer for AI-Generated Pocket Definitions

## Purpose

When AI agents generate pocket definitions (rippleSpecs), the output format varies widely. An agent might produce a flat widget list, a UISpec v1.0 structure, or a multi-pane layout — each with different fields present or absent. The `normalize_ripple_spec` function serves as a defensive normalization layer that ensures every spec has the minimum required envelope fields before it hits the database.

Without this normalizer, the frontend would need to handle dozens of possible missing-field combinations, leading to rendering crashes and undefined behavior.

## How It Works

### Envelope Construction

Every spec gets a standard envelope applied:
- **lifecycle** — defaults to `{"type": "persistent", "id": "<generated>"}` if missing
- **title / name** — extracted from either field, cross-populated
- **color** — falls back through `spec.color` → `metadata.color` → `#0A84FF` (PocketPaw brand blue)
- **metadata.category** — defaults to `"custom"`

The pocket ID follows a fallback chain: `spec.id` → `lifecycle.id` → auto-generated `pocket-<8 hex chars>`.

### Format Detection and Pass-Through

The normalizer detects three spec formats and handles each differently:

1. **Multi-pane specs** (`spec.panes` is a dict) — pass through with envelope overlay and version defaulting to `"1.0"`
2. **UISpec v1.0** (`spec.ui` is a dict with a `type` field) — same pass-through treatment
3. **Flat widget lists** (`spec.widgets` is a non-empty list) — widgets get auto-generated IDs (`{pocket_id}-w{index}`) and titles if missing, plus layout defaults (`columns: 3`, grid gap)

If none of these formats match, the spec is returned as-is with just the envelope applied.

### ID Generation

`_short_id()` uses `secrets.token_hex(4)` to produce 8-character random hex strings. This is used for auto-generated pocket IDs when the AI omits them. The `secrets` module is chosen over `random` because it provides cryptographically strong randomness, though collision resistance is the real motivation here — not security.

### Defensive Guards

- `if not spec or not isinstance(spec, dict): return None` — prevents crashes when the AI returns empty strings, None, or non-dict values
- Non-dict widgets are silently skipped (`if not isinstance(w, dict): continue`) — handles malformed AI output gracefully
- All `.get()` calls use `or` fallbacks rather than default parameters, so empty strings are treated the same as missing fields

## Known Gaps

- No schema validation beyond structural checks — a spec with nonsensical field values will pass through
- The version field defaults differently per format (`"1.0"` for multi-pane/UISpec, `"2.0"` for flat widgets) with no documentation explaining why
- No logging when normalization fills in missing fields, making debugging AI output issues harder
