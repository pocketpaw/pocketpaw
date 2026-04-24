---
{
  "title": "Connector Status Route Tests: State Transitions, Pocket Isolation, and Secret Non-Leakage",
  "summary": "This test file locks down PocketPaw's connector status route contract — the full state machine from disconnected through connected to expired, strict per-pocket credential isolation so one pocket cannot see another's connection state, and the guarantee that OAuth secrets passed at connect time never appear in status responses.",
  "concepts": [
    "ConnectorRegistry",
    "connector status",
    "pocket-scoped isolation",
    "cred_state",
    "connected state",
    "OAuth secret non-leakage",
    "record_connector_event",
    "state machine",
    "_STATUS_EXTRAS",
    "disconnect",
    "Google Drive connector"
  ],
  "categories": [
    "connectors",
    "security",
    "API",
    "testing",
    "test"
  ],
  "source_docs": [
    "bdf2959a80b2fb77"
  ],
  "backlinks": null,
  "word_count": 541,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's connector system lets AI pockets integrate with external services such as Google Drive. The status route (`GET /connectors/{name}/status`) is a read surface that the dashboard polls to render connection badges. Because it is read-only and frequently called, its contract is especially important: it must reflect the true state without leaking credentials or mixing up state across pockets.

This file was created as part of Cluster C / PR2 (gap C5 in `docs/plans/cluster-C-reality.md`) specifically to lock the status transition contract and pocket-scoped isolation.

## Fake Registry

`_FakeRegistry` implements the subset of `ConnectorRegistry` that the router touches: `get_definition`, `get_adapter`, `connect`, `disconnect`, `status`, and `available`. It stores instances in a `dict` keyed by `"{pocket_id}:{connector_name}"`, which is precisely how pocket-scoping works in the real registry. This design means the isolation tests are not just mocking — they exercise the actual scoping logic.

## Autouse Reset Fixture

```python
@pytest.fixture(autouse=True)
def _reset_extras():
    connectors_module._STATUS_EXTRAS.clear()
    yield
    connectors_module._STATUS_EXTRAS.clear()
```

`_STATUS_EXTRAS` is a module-level dict used by `record_connector_event` to inject OAuth state overrides (e.g. `cred_state: "expired"`) without requiring a full re-connect. Clearing it before and after each test prevents state leakage between tests, which would cause the `test_expired_state_is_surfaced` test to affect other tests if they ran in the wrong order.

## State Transition Tests

- **Unknown connector is 404**: A connector name not in the registry returns 404, not an empty status object. This prevents the client from treating a typo as a valid disconnected connector.
- **Disconnected default**: A known connector that has never been connected returns `connected: false`, `cred_state: "missing"`, `scope: ""`, and `last_sync: null` — the zero state.
- **Connected transitions cred_state**: After `POST /connectors/connect`, the status for that pocket shows `connected: true`, `cred_state: "valid"`, the requested scope, and a non-null ISO timestamp for `last_sync`.
- **Disconnect returns to missing**: After `POST /connectors/disconnect`, the status reverts to the zero state — `cred_state: "missing"`, empty scope, `connected: false`.

## Pocket-Scoped Isolation

```python
def test_status_is_pocket_scoped(self, client):
    """A pocket with no connection must NOT inherit another pocket's state."""
```

This is the most critical test in the file. Pocket `alpha` connects to Google Drive; pocket `beta` does not. The test asserts that `beta`'s status shows `connected: false` and `cred_state: "missing"` — it must not inherit `alpha`'s connected state. Without this isolation, one user's connected account could bleed into another user's view in a multi-tenant deployment.

## Expired State Surfacing

`record_connector_event` lets the OAuth refresh path flip the UI badge to `"expired"` without triggering a full reconnect. The test calls this helper directly and asserts the status endpoint reflects `cred_state: "expired"` even though no adapter instance exists.

## Secret Non-Leakage

```python
def test_config_secrets_never_leak_into_status(self, client):
```

The connect payload includes `client_secret` and `refresh_token`. After connecting, the status response must not contain these fields — neither as top-level keys nor embedded anywhere in the serialised body. This is the core security contract: the connect endpoint accepts credentials, stores them internally, and the status endpoint exposes only derived, non-sensitive state.

## Known Gaps

No `TODO` or `FIXME` markers are present. The fake registry does not simulate connect failures, so the 400/500 error paths of `POST /connectors/connect` are not covered here. The `expired` test confirms the state can be set but does not test the OAuth refresh flow that would normally trigger it.