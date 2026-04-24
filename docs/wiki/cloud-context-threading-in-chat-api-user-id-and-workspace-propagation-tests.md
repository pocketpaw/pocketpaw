---
{
  "title": "Cloud Context Threading in Chat API: User ID and Workspace Propagation Tests",
  "summary": "Tests that `resolve_cloud_context` correctly injects `cloud_user_id` and `cloud_workspace_id` into `InboundMessage.metadata` when an authenticated cloud user is present, and leaves those keys absent for anonymous or unauthenticated requests. The test file operates at the unit level — directly calling `_build_inbound_message` and `_send_message` — to avoid the overhead of standing up a full SSE streaming client.",
  "concepts": [
    "cloud context",
    "resolve_cloud_context",
    "InboundMessage.metadata",
    "cloud_user_id",
    "cloud_workspace_id",
    "message bus",
    "tenant isolation",
    "FastAPI dependency injection",
    "monkeypatching",
    "stub fixtures"
  ],
  "categories": [
    "testing",
    "chat API",
    "cloud integration",
    "multi-tenant",
    "test"
  ],
  "source_docs": [
    "4a7cce4df3e1a820"
  ],
  "backlinks": null,
  "word_count": 447,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_api_chat_cloud_context.py` tests the cloud identity threading mechanism in PocketPaw's chat router. When PocketPaw is deployed as a cloud service (rather than locally), each HTTP request may carry a resolved cloud user (via JWT or session cookie). The chat router needs to forward that identity into every message published to the internal event bus so downstream agent handlers and audit logs know which cloud user initiated the conversation.

## Why Cloud Context Threading Exists

Without cloud context propagation, the agent runtime sees only an anonymous message. Downstream handlers cannot enforce per-user quotas, tenant isolation, or audit trails. If the threading is broken, a cloud user's messages silently appear as anonymous, which is both a security gap (no audit trail) and a billing gap (no user attribution).

## resolve_cloud_context Dependency

`test_resolve_cloud_context_returns_none_pair_for_null_user` verifies the FastAPI dependency `resolve_cloud_context` yields `(None, None)` when no authenticated user is present. This is the default case for local PocketPaw instances — the absence of cloud context must not cause the router to crash or inject garbage values.

`test_resolve_cloud_context_returns_ids_for_authenticated_user` verifies the happy path: a resolved user object causes the dep to yield a `(user_id, active_workspace)` tuple.

## _build_inbound_message Metadata Injection

Three tests exercise `_build_inbound_message` directly:

- **Both keys present** — `test_build_inbound_message_writes_cloud_keys_when_present` confirms that a non-empty `cloud_ctx` results in both `cloud_user_id` and `cloud_workspace_id` appearing in the built message's metadata.
- **Partial context** — `test_build_inbound_message_omits_missing_keys` covers the case where only `user_id` is set but no workspace. The built message should carry only `cloud_user_id` and leave `cloud_workspace_id` absent rather than setting it to `None`.
- **Empty context** — `test_build_inbound_message_default_cloud_ctx_is_empty` proves the default (no cloud context) leaves metadata clean — no `cloud_*` keys at all.

**Failure scenario prevented:** Setting a key to `None` versus omitting it entirely has different implications for downstream consumers that use `metadata.get("cloud_user_id")` vs `"cloud_user_id" in metadata`. Explicit tests for both cases prevent subtle conditional logic bugs in audit handlers.

## _send_message Bus Publication

Two tests use the `stub_bus` fixture to capture the `InboundMessage` published to the bus without routing it through the real message loop:

- `test_send_message_propagates_cloud_ctx_to_bus` — confirms cloud context flows through `_send_message` to the published message.
- `test_send_message_without_cloud_ctx_publishes_clean_metadata` — confirms the absence of cloud context results in clean metadata (no `cloud_*` keys).

The `stub_bus` fixture monkeypatches `publish_inbound` to store the message in a `SimpleNamespace` so assertions can inspect it without any async bus infrastructure.

The `stub_resolver` fixture short-circuits media resolution so `_build_inbound_message` doesn't require real upload URL infrastructure during these unit tests.

## Known Gaps

No TODO or FIXME markers are present. The test file was created 2026-04-22 and focuses exclusively on cloud context propagation — it does not cover error cases where `resolve_cloud_context` raises an exception rather than yielding `(None, None)`.