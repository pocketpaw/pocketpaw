---
{
  "title": "UploadResolver: Bridging Upload URLs to Local Agent Paths",
  "summary": "UploadResolver translates frontend upload URLs (e.g., `/api/v1/uploads/{id}`) into local filesystem paths that the agent loop can inject into Read tool calls or image blocks. It is the OSS-only half of a two-track design, with EE supplying a workspace-scoped variant backed by MongoDB.",
  "concepts": [
    "UploadResolver",
    "ResolvedMedia",
    "FileRecord",
    "resolve_media_with_records",
    "resolve_media_paths_any",
    "default_resolver",
    "agent loop",
    "media injection",
    "OSS two-track",
    "metadata store"
  ],
  "categories": [
    "uploads",
    "agent integration"
  ],
  "source_docs": [
    "f41771badf5b0098"
  ],
  "backlinks": null,
  "word_count": 559,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

When a user sends a media file through the chat UI, the frontend stores it via the upload API and embeds a relative URL—like `/api/v1/uploads/abc123`—into the message payload. The agent loop, however, consumes file system paths: the `Read` tool needs an absolute path, and image blocks in the Anthropic API need a base64-encoded byte stream read from disk. `UploadResolver` is the bridge that makes this translation reliable and transparent.

## Why a Dedicated Resolver?

Without this module, every agent loop invocation would need to know where uploads live on disk, how keys are structured, and how to recover gracefully when a URL refers to a file that hasn't finished uploading or doesn't exist. Centralising that logic into `UploadResolver` means the agent loop only calls `resolve_media_with_records` and iterates the results—it never touches the storage adapter directly.

## OSS vs. EE Track

A comment at the module level flags this explicitly: `OSS-only — EE has its own workspace-scoped resolver alongside its Mongo store.` In the Enterprise Edition, uploads are scoped to workspaces and stored in MongoDB alongside richer metadata. The OSS resolver wires directly to the file store singleton and the local or S3 adapter, keeping the open-source path simple without imposing the EE data model.

## ResolvedMedia Dataclass

`ResolvedMedia` bundles two things: the local path string the agent loop will inject, and the `FileRecord` from the metadata store. The `FileRecord` is preserved because callers occasionally need original filename, MIME type, or owner ID for audit logging and access-control decisions—not just the raw bytes.

## Resolution Flow

1. Incoming media entries are tested against a URL pattern (`/api/v1/uploads/{id}` or similar).
2. Matching entries have their ID extracted and used to query the metadata store.
3. A metadata hit produces a `FileRecord`, from which the storage key is derived.
4. The adapter's `exists` check confirms the blob is present on disk before the path is returned.
5. Non-matching or missing entries are silently dropped; the caller receives only successfully resolved paths.

The silent-drop behaviour is intentional: a user message might embed a mix of resolved and unresolved media (e.g., an external URL and an upload URL). Failing the whole message because one attachment is missing would break the agent for an avoidable reason.

## Public Async Helpers

Two public async helpers exist for different call sites. `resolve_media_paths_any` returns just a `list[str]` of paths—suitable for simple injection into tool arguments. `resolve_media_with_records` returns `list[ResolvedMedia]`—used by call sites that need MIME type or ownership for downstream policy checks.

## `default_resolver` Factory

`default_resolver()` wires the OSS singletons together and caches the result, so callers don't need to know which adapter is active:

```python
resolver = default_resolver()
records = await resolve_media_with_records(message.media)
for r in records:
    # r.path is ready for the agent loop
    # r.record.mime_type is available for image-block construction
```

## Known Gaps

- The `_MetaReader` Protocol is defined but never exported. If a caller wants to pass a custom metadata reader (e.g., in tests), they must instantiate `UploadResolver` directly rather than relying on structural subtyping through a public protocol.
- The resolver does not handle upload IDs that are valid UUIDs but refer to records belonging to a different tenant in OSS multi-user setups. Access-control decisions are deferred entirely to the caller.
- No retry logic exists for transient `exists` failures—a momentary I/O error returns the same result as a genuinely missing file.
