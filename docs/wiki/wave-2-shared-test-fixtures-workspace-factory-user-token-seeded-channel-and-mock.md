---
{
  "title": "Wave 2 Shared Test Fixtures: Workspace Factory, User Token, Seeded Channel, and Mock S3 Smoke Tests",
  "summary": "Smoke tests that validate the four shared pytest fixtures defined in `tests/ee/conftest.py` for the Wave 2 ee/cloud test infrastructure. Each test exercises exactly one factory (workspace creation, user authentication, channel seeding, and in-memory S3), proving the conftest wiring works end-to-end before the feature-level tests that depend on it are run.",
  "concepts": [
    "shared fixtures",
    "conftest",
    "workspace_factory",
    "user_token_pair",
    "seeded_channel",
    "mock_s3",
    "moto",
    "mongomock-motor",
    "beanie",
    "FastAPI TestClient",
    "Wave 2"
  ],
  "categories": [
    "testing",
    "test infrastructure",
    "enterprise features",
    "fixtures",
    "test"
  ],
  "source_docs": [
    "bcb794c0e9c0470f"
  ],
  "backlinks": null,
  "word_count": 443,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/ee/test_shared_fixtures.py` was created in `feat/wave-2-pytest-fixtures-v2` as a self-test for the shared conftest factories. It deliberately keeps scope narrow — one assertion per factory — so that when a factory breaks, the failure points directly at the infrastructure layer rather than surfacing as a confusing failure deep inside a feature test.

## Why Self-Test the Fixtures?

The ee/cloud test suite uses four session-scoped or function-scoped factories from `tests/ee/conftest.py`:
- `user_token_pair`: registers a user, logs in, returns `{email, user_id, headers}`.
- `workspace_factory`: creates and activates a workspace for a given user.
- `seeded_channel`: creates a channel and posts N messages, returning `(channel_id, [message_ids])`.
- `mock_s3`: a moto-backed in-memory S3 client injected into the upload adapter.

All four are callable factories (not plain fixtures) so individual tests can control count and identity. If any factory silently fails to persist — for example, a Beanie document that inserts without error but cannot be retrieved — a feature test that calls the factory and then immediately reads via the API would see a 404 and have no obvious path to the root cause. These smoke tests surface that class of breakage as an immediate fixture failure.

## Test Breakdown

### test_workspace_factory_creates_and_cleans_up
Mints a workspace, GETs it by id via the API, and asserts the active workspace on the current user matches. Two field names are accepted (`active_workspace` and `activeWorkspace`) because the serialisation key was renamed between API versions and both may appear in the wild.

Teardown is intentionally implicit — mongomock-motor throws away its in-memory database when the `beanie_test_db` fixture unwinds at session end. The test does not call a delete endpoint; a regression in deletion logic is out of scope.

### test_user_token_pair_returns_usable_token
Calls `GET /api/v1/auth/me` with the bearer token the factory returned and verifies both `email` and `id` round-trip. This catches a factory that creates a user but returns stale credentials or a mismatched user id.

### test_seeded_channel_has_expected_messages
Seeds five messages, calls `GET /api/v1/chat/groups/{channel_id}/messages`, and checks that the returned id set matches the factory-reported ids exactly (order-insensitive). The payload shape is handled defensively: the endpoint may return a plain list or an envelope `{items: [...]}`, and the test unwraps either.

### test_mock_s3_put_and_get_roundtrip
```python
def test_mock_s3_put_and_get_roundtrip(mock_s3) -> None:
    bucket = f"shared-fixture-{uuid.uuid4().hex[:8]}"
    mock_s3.create_bucket(Bucket=bucket)
    mock_s3.put_object(Bucket=bucket, Key="hello.txt", Body=b"wave-2-fixtures")
    obj = mock_s3.get_object(Bucket=bucket, Key="hello.txt")
    assert obj["Body"].read() == b"wave-2-fixtures"
```

A unique bucket name per run prevents false positives from a stale moto state leaking across test sessions.

## Known Gaps

The `seeded_channel` teardown is not tested — if the channel persists beyond the test, subsequent tests that assert a clean database state could be affected. No test exercises the factory with `count=0` (edge case) or with a non-existent workspace (error path).