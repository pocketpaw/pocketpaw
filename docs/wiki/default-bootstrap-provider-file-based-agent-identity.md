---
{
  "title": "Default Bootstrap Provider — File-Based Agent Identity",
  "summary": "DefaultBootstrapProvider reads the agent's identity, soul, style, instructions, and user profile from markdown files in the PocketPaw config directory, caching each file's content keyed by modification time to avoid redundant disk reads. It is the standard provider for self-hosted and developer deployments where identity is managed as local configuration rather than a database record.",
  "concepts": [
    "DefaultBootstrapProvider",
    "identity files",
    "IDENTITY.md",
    "mtime cache",
    "_identity_file_cache",
    "config directory",
    "get_config_dir",
    "non-UTF-8 handling",
    "default instructions",
    "BootstrapContext",
    "file-based identity"
  ],
  "categories": [
    "bootstrap",
    "agent-identity",
    "configuration"
  ],
  "source_docs": [
    "0000000000000003"
  ],
  "backlinks": null,
  "word_count": 401,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Purpose

When PocketPaw runs as an OSS self-hosted deployment, there is no MongoDB record defining who the agent is. Instead, the operator edits a set of markdown files in the config directory (`IDENTITY.md`, `SOUL.md`, `STYLE.md`, `INSTRUCTIONS.md`, `USER.md`, `KNOWLEDGE.md`). `DefaultBootstrapProvider` reads those files and assembles them into a `BootstrapContext` that the rest of the runtime can consume without knowing the source.

## File-Based Identity Cache

The module maintains a module-level `_identity_file_cache` dict mapping file paths to `_IdentityCache` instances that store both the file content and its last-modification timestamp (`mtime`). Each call to `_read_identity_file` checks the current `mtime` against the cached value:

- If the file has not changed, the cached content is returned immediately — no disk I/O.
- If the `mtime` differs (the operator edited the file), the file is re-read and the cache is updated.
- If the file does not exist, an empty string is returned silently.

This design lets operators update identity files at runtime without restarting the server. The mtime check is cheap (a single `stat()` call) and the cache prevents redundant reads in high-throughput scenarios where multiple agent turns fire in quick succession.

### Non-UTF-8 Handling

Files are read as bytes and decoded with `errors="replace"`, converting undecodable bytes to the Unicode replacement character `\ufffd`. A warning is logged if any replacements occur. This prevents a crash if the operator accidentally saves a file with a non-UTF-8 encoding (e.g., Windows-1252 from a text editor), while flagging the problem so it can be fixed.

## Default Instructions

If no `INSTRUCTIONS.md` file is present, `_DEFAULT_INSTRUCTIONS` provides a built-in fallback that documents the core tools available to the agent (Shell, Read/Write/Edit, Glob/Grep, WebSearch, WebFetch, remember/forget). This ensures a freshly installed PocketPaw instance is functional out of the box: the agent knows what tools it has even without a custom instructions file.

## Config Directory Resolution

The provider calls `get_config_dir()` to locate the root directory containing identity files. This function centralises the platform-specific logic for finding the right directory (e.g., `~/.config/pocketpaw` on Linux, `~/Library/Application Support/PocketPaw` on macOS), keeping the provider free of path-handling concerns.

## Known Gaps

The `KNOWLEDGE.md` file, if it exists, is read as a block of free-form text rather than parsed into structured bullet points. The `BootstrapContext.knowledge` field expects a list, so the provider currently wraps the entire file content in a single-item list. This loses the per-item structure that `AgentContextBuilder` could otherwise use for smarter truncation.