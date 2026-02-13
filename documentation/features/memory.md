# Memory System

PocketPaw's memory system provides long-term fact storage, session history with smart compaction, and a user profile loaded into every conversation.

## Session History Compaction

Long conversations are automatically compacted to stay within a character budget, keeping recent messages verbatim while summarizing older ones.

### How It Works

1. **Recent window** — The last N messages (default: 10) are kept verbatim
2. **Older messages** — Compacted using one of two tiers:
   - **Tier 1 (Extracts)** — Each older message is truncated to a one-liner (default: 150 chars max)
   - **Tier 2 (LLM Summary)** — Older messages are summarized by Haiku into a concise paragraph (opt-in)
3. **Budget enforcement** — Total compacted history stays within `compaction_char_budget` (default: 8,000 chars)

### Tier Selection

- Tier 2 (LLM) is used when `compaction_llm_summarize` is `true` AND an Anthropic API key is available
- Otherwise, Tier 1 (extracts) is used as the default fallback
- Compaction results are cached at `~/.pocketclaw/memory/sessions/{session_key}_compaction.json`

### Configuration

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `compaction_recent_window` | int | `10` | Number of recent messages to keep verbatim |
| `compaction_char_budget` | int | `8000` | Max total characters for compacted history |
| `compaction_summary_chars` | int | `150` | Max characters per one-liner extract (Tier 1) |
| `compaction_llm_summarize` | bool | `false` | Use Haiku to summarize older messages (Tier 2) |

### Example

With default settings and a 50-message conversation:
- Messages 41–50 → kept verbatim
- Messages 1–40 → each compressed to ≤150 chars, or summarized as a block by Haiku

---

## USER.md Profile

PocketPaw maintains identity files at `~/.pocketclaw/identity/` that are loaded into the system prompt for every conversation.

### Identity Files

| File | Purpose |
|------|---------|
| `IDENTITY.md` | Agent name and role |
| `SOUL.md` | Personality and tone |
| `STYLE.md` | Response formatting preferences |
| `USER.md` | User profile — preferences, context, working environment |

### Auto-Creation

On first run, `DefaultBootstrapProvider` creates default versions of all four files if they don't exist. Users can edit them to customize the agent's behavior.

### How It's Used

The `BootstrapContext` assembles the system prompt from these files:
- `identity` — from IDENTITY.md
- `soul` — from SOUL.md
- `style` — from STYLE.md
- `user_profile` — from USER.md

This means the agent always knows your preferences, project context, and communication style.

---

## Long-term Memory

### File-based (Default)

Stores memories as markdown in `~/.pocketclaw/memory/`:
- `MEMORY.md` — Long-term facts (e.g., "User prefers dark mode")
- `sessions/` — Session history files

Key operations:
- `remember(fact)` — Save a fact to MEMORY.md
- `recall(query)` — Search memories by keyword
- `note(text)` — Add a session note

### Mem0 (Optional)

Semantic memory with vector search and automatic fact extraction:

```bash
pip install pocketpaw[memory]
```

Configure:
```json
{
  "memory_backend": "mem0",
  "memory_use_inference": true
}
```

Mem0 features:
- Semantic search (find related memories, not just keyword matches)
- Automatic fact extraction from conversations
- Memory evolution (updates facts instead of duplicating)

> **Note:** Mem0 requires embeddings. By default it uses OpenAI embeddings (needs `OPENAI_API_KEY`). For fully local, configure a local embedding model.

---

## Implementation

| File | Description |
|------|-------------|
| `memory/manager.py` | `MemoryManager` — high-level facade with `get_compacted_history()` |
| `memory/protocol.py` | `MemoryStoreProtocol` — interface for swappable backends |
| `bootstrap/default_provider.py` | `DefaultBootstrapProvider` — identity file loading + USER.md |
| `bootstrap/protocol.py` | `BootstrapContext` — system prompt assembly |

## Tests

- `tests/test_compaction.py` — Session compaction tests
- `tests/test_user_profile.py` — USER.md profile loading tests
