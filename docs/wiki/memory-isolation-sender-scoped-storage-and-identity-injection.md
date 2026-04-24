---
{
  "title": "Memory Isolation: Sender-Scoped Storage and Identity Injection",
  "summary": "Tests verifying that PocketPaw's MemoryManager correctly isolates memories by sender identity, so conversations from different users do not bleed into each other. Covers user ID resolution, per-user file routing in FileMemoryStore, and identity injection into system prompts.",
  "concepts": [
    "memory isolation",
    "sender-scoped memory",
    "user ID resolution",
    "FileMemoryStore",
    "MemoryManager",
    "identity injection",
    "system prompt",
    "SHA hash",
    "multi-user",
    "backward compatibility",
    "file routing",
    "PII protection"
  ],
  "categories": [
    "memory system",
    "security",
    "testing",
    "multi-user",
    "test"
  ],
  "source_docs": [
    "1bfc3298ce33075b"
  ],
  "backlinks": null,
  "word_count": 568,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Multi-user agents need memory isolation. Without it, a message sent by User A might surface in User B's context — a privacy violation and a correctness bug. The `test_memory_isolation.py` suite pins down the contracts that prevent this, focusing on three layers: user ID resolution, file-system routing, and system-prompt identity injection.

## User ID Resolution (`TestResolveUserId`)

`MemoryManager._resolve_user_id()` maps an incoming `sender_id` to either `"default"` (the owner's namespace) or a deterministic hash (for any other user). The tests establish these invariants:

- **No sender → default**: `None` or `""` sender falls back to the owner namespace. This preserves backward compatibility with single-user deployments that never pass a sender.
- **No owner configured → default**: If `owner_id` is not set in settings, all senders collapse to `"default"`. This prevents a crash when the system is run without an owner configured.
- **Sender is owner → default**: The owner's own messages use the owner namespace, not a hashed one, so the owner sees their full memory context.
- **Non-owner → SHA hash**: Every other sender gets a deterministic, collision-resistant hash. The `test_hash_is_deterministic` test confirms the same sender always hashes to the same ID, which is critical for recalling memories across sessions. `test_different_senders_different_hashes` confirms two senders never collide.

The hash approach was chosen over plain sender IDs to avoid storing raw phone numbers or user handles in file paths (a PII leak and a path-injection risk).

## File-System Routing (`TestFileStoreUserScoping`)

`FileMemoryStore` stores memories in `MEMORY.md` files. For multi-user support it routes per-user long-term memories to `MEMORY-{user_id}.md` while keeping daily notes in a single global file.

Key test scenarios:
- `test_get_user_memory_file_default` / `test_get_user_memory_file_non_default`: Assert that the file path changes based on user ID.
- `test_save_long_term_default_user` / `test_save_long_term_scoped_user`: Confirm that memories saved for different users land in different files, preventing cross-user reads.
- `test_daily_notes_stay_global`: Daily notes are intentionally shared across users (they represent the agent's own journal, not user-specific data).
- `test_load_index_includes_user_files`: The index loader must discover and include all user-scoped files, so recall searches across all users when operating as the owner.
- `test_parsed_user_id_from_path`: The system can reverse-parse a file path back to a user ID, enabling cleanup and audit operations.

## Integration Tests (`TestMemoryManagerScoping`)

These end-to-end tests wire together `MemoryManager` with a real `FileMemoryStore` in a temp directory:

- `test_remember_sets_user_id`: Confirms the full save path writes to the user-scoped file.
- `test_remember_owner_uses_default`: The owner's memories always go to the default namespace regardless of what `sender_id` is provided.
- `test_get_context_scoped`: Recall for a non-owner only surfaces that user's memories, not the owner's.
- `test_backward_compat_no_sender`: An agent that never passes `sender_id` still works — all memories go to `"default"`.

## Identity Injection (`TestContextBuilderIdentity`)

When the agent builds its system prompt, it injects an identity block describing who is currently speaking. Tests confirm:
- Owner gets their configured name and role in the identity block.
- Non-owner gets a neutral identity block based on their hashed user ID.
- No `sender_id` → the default (owner) identity is used.

This injection matters because the underlying LLM otherwise has no awareness of who is sending a message, which could cause it to apply the wrong persona or permissions.

## Known Gaps

No TODOs or FIXMEs were found in this test file. However, the suite does not test concurrent writes from two senders simultaneously — a potential race condition where both users' data could be written to the wrong file if the store is not properly locked.
