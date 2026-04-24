---
{
  "title": "Upload Read Authorization Tests: Owner, Chat Member, Workspace Admin, and Existence-Hiding",
  "summary": "Tests for `EEUploadService._assert_can_read`, the read gate used by `stream`, `presigned_get`, and the `/download-url` and `/grant` endpoints. The parametrized suite verifies that the owner, chat members, and workspace admins can read while strangers receive `NotFound` (not `Forbidden`) — an existence-hiding design that prevents file-ID probing.",
  "concepts": [
    "_assert_can_read",
    "EEUploadService",
    "read gate",
    "existence-hiding",
    "NotFound",
    "chat member",
    "workspace admin",
    "owner authorization",
    "stream",
    "presigned_get",
    "collaborator checkers"
  ],
  "categories": [
    "testing",
    "uploads",
    "authorization",
    "security",
    "test"
  ],
  "source_docs": [
    "b18a5f116b068f5e"
  ],
  "backlinks": null,
  "word_count": 435,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`EEUploadService._assert_can_read` is the central authorization check for every file read in the EE upload service. It must allow three categories of requesters:

1. The file **owner** (always)
2. A **chat member** — any user who is a member of the chat the file was uploaded into
3. A **workspace admin/owner** — who has administrative visibility into all files

Everyone else receives `NotFound` rather than `Forbidden`. This existence-hiding design ensures that an attacker who guesses a file ID cannot confirm the file exists (a 403 would do so; a 404 does not).

## Parametrized Gate Tests

The main parametrized test `test_read_gate_allows_owner_member_admin_denies_others` uses four cases:

```python
("owner", set(), set(), True)       # owner always allowed
("peer", {"peer"}, set(), True)     # chat member allowed
("boss", set(), {"boss"}, True)     # workspace admin allowed
("stranger", set(), set(), False)   # denied → NotFound
```

Each case constructs `_make_checkers` with the appropriate sets of authorized users, builds an `EEUploadService` with those checkers wired in, uploads a file as `"owner"`, then attempts to stream and presigned-get as the requester. Both read paths (`stream` and `presigned_get`) are tested to ensure the gate is applied consistently across all read surfaces.

## _MemAdapter

An in-memory `StorageAdapter` subclass that stores blobs in a dict. This avoids any filesystem dependency in the read-auth tests, keeping them pure unit tests. The adapter's `open` method yields stored bytes and raises `NotFound` for missing keys.

## Without-Checkers Backward Compatibility

`test_read_gate_without_checkers_is_owner_only` creates an `EEUploadService` with no `is_chat_member` or `is_workspace_admin` callbacks wired. The test confirms a `"peer"` requester still receives `NotFound`. This preserves the pre-feature contract: callers that do not wire collaborator checkers get owner-only access, unchanged. This is critical for the OSS tier or any deployment that does not configure collaborator checks.

## No chat_id Edge Case

`test_chat_member_branch_skipped_when_no_chat_id` uploads a file without a `chat_id` (e.g., an avatar or knowledge-base upload). The test ensures that the chat-member branch of the gate is skipped entirely when `chat_id` is absent — no chat membership check is performed. Without this guard, a `None` chat ID passed to the `is_chat_member` callback could cause an error or incorrectly grant access.

## Existence-Hiding Design

The choice to raise `NotFound` (instead of `Forbidden`) for unauthorized requesters is deliberate. An attacker cannot distinguish between "this file does not exist" and "this file exists but you cannot access it." This is the same pattern used by the workspace-isolation tests in `test_router.py` and `test_download_url.py`.

## Known Gaps

There are no tests covering the behavior when the `is_chat_member` or `is_workspace_admin` callbacks themselves raise an exception (e.g., if the collaborator service is down). The current implementation likely propagates the exception, but this is untested.
