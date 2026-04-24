---
{
  "title": "API Keys Panel Acceptance Tests: UI Lifecycle Flows for Cluster F",
  "summary": "End-to-end acceptance tests that walk the exact flows the `ApiKeysPanel.svelte` UI executes — create, list, rotate (with old key invalidation), revoke, and sort-by-creation. These tests complement unit-level coverage by pinning the API shape the frontend depends on, catching regressions that break the dashboard silently.",
  "concepts": [
    "acceptance testing",
    "ApiKeysPanel",
    "UI lifecycle flow",
    "key rotation atomicity",
    "old key invalidation",
    "plaintext key",
    "sort by creation",
    "Cluster F",
    "frontend regression",
    "monkeypatch singleton"
  ],
  "categories": [
    "testing",
    "API key management",
    "acceptance testing",
    "dashboard UI",
    "test"
  ],
  "source_docs": [
    "d96c85173727d380"
  ],
  "backlinks": null,
  "word_count": 445,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_api_keys_cluster_f.py` is a targeted acceptance test file created specifically to prevent regressions in the API key management panel of PocketPaw's dashboard UI. The file is named "Cluster F" following PocketPaw's convention of grouping related acceptance tests by feature cluster.

## Why These Tests Exist

The unit tests in `test_api_keys.py` verify the `APIKeyManager` class in isolation. These Cluster F tests operate at the HTTP layer and mirror the exact sequence of API calls the `ApiKeysPanel.svelte` frontend makes. Two specific UI behaviors motivated the creation of this file:

1. **Plaintext key is shown only once at creation.** The panel copies the key to the clipboard when the create response arrives. If a rotate response fails to return the new plaintext key, the user has no way to retrieve it — the key is lost.
2. **Rotate must atomically invalidate the old key.** If rotation creates a new key without immediately invalidating the old one, there is a window where both keys are valid. This is a security gap in multi-user or multi-device setups.

## test_create_list_rotate_old_key_fails_verification

This is the primary journey test. It walks the panel's full lifecycle in a single test:

1. `POST /api/v1/api_keys` — creates a key, captures the plaintext.
2. `GET /api/v1/api_keys` — lists keys, verifies the new key appears without plaintext.
3. `POST /api/v1/api_keys/{id}/rotate` — rotates, captures the new plaintext.
4. Calls `manager.verify(old_plaintext_key)` directly — asserts it fails.

The direct manager verification in step 4 is intentional: it proves the old key is truly invalidated at the storage layer, not just absent from the list response.

## test_revoke_panel_flow

Tests the revoke-from-list flow: create, list to get the ID, revoke by ID, then attempt a second list to confirm the key is gone. This mirrors the dashboard's "Delete" button flow.

**Failure it prevents:** A revoke endpoint that returns 200 but doesn't actually persist the deletion would pass HTTP-level assertions while leaving the key active — only a subsequent list check catches this.

## test_list_response_is_sorted_by_creation

Creates three keys in sequence and verifies `GET /api/v1/api_keys` returns them sorted newest-first. The panel renders keys in this order. If the sort order is wrong, the user sees an inconsistent list that doesn't match creation order.

## Fixture Design

The `client` fixture uses `monkeypatch` to replace the module-level `_manager` singleton in `pocketpaw.api.api_keys` with a temp-dir-backed manager. This technique avoids modifying production state while allowing the full HTTP layer (including dependency injection and request validation) to execute normally.

## Known Gaps

No TODO or FIXME markers. The tests do not cover error responses when rotation is called on an already-revoked key, or the behavior when the storage file is corrupted between create and rotate. Concurrent rotation is also untested.