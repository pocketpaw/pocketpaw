---
{
  "title": "Firebase Connector: YAML Definition, CLI Adapter, and Trust Level Tests",
  "summary": "This test suite validates PocketPaw's Firebase connector from three angles: the declarative YAML definition (action count, trust levels, auth method), the `FirebaseAdapter` class that wraps the Firebase CLI via async subprocess, and the connector registry integration. It uses mocked subprocess calls to test connect, execute, timeout, and error paths without touching a real Firebase project.",
  "concepts": [
    "Firebase connector",
    "YAML connector definition",
    "FirebaseAdapter",
    "trust levels",
    "CLI subprocess",
    "async subprocess",
    "connector registry",
    "timeout handling",
    "TrustLevel",
    "ConnectorStatus",
    "LOCAL method"
  ],
  "categories": [
    "testing",
    "connectors",
    "Firebase",
    "CLI integration",
    "test"
  ],
  "source_docs": [
    "76b29a4d3a583412"
  ],
  "backlinks": null,
  "word_count": 521,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw integrates with Firebase through a connector that wraps the `firebase` CLI binary. Rather than using Firebase's Python SDK directly, the adapter shell-execs `firebase` commands, captures their JSON output, and normalizes results into PocketPaw's connector protocol. This approach avoids Python dependency management for Firebase's SDK and works with any Firebase project that has the CLI authenticated.

## YAML Definition Tests (`TestFirebaseYAML`)

The Firebase connector is declared in `connectors/firebase.yaml`. These tests parse that file via `parse_connector_yaml` and assert its structure:

- **16 actions** are defined, covering Firestore CRUD, hosting, functions, auth, and remote config.
- **All actions use `method: LOCAL`**, meaning they execute via CLI on the machine running PocketPaw — not via a remote API call.
- **Trust levels** are a core safety mechanism. Destructive actions (`firestore_delete`, `firestore_export`, `hosting_deploy`, `functions_deploy`, `auth_import_users`) require either `confirm` or `restricted` trust, preventing autonomous agents from deploying or deleting without human approval. Read-only actions (`list_projects`, `firestore_get`, `auth_list_users`, etc.) are `auto`, meaning they can execute without approval.
- **Auth method is `none`** — authentication is handled by the Firebase CLI's existing login session, not by PocketPaw storing credentials.

## Adapter Connection Tests (`TestFirebaseAdapterConnect`)

`test_connect_success` mocks `shutil.which` to return a valid path and mocks `asyncio.create_subprocess_exec` to return a successful `firebase projects:list --json` response. This verifies the adapter parses the project list and transitions to `ConnectorStatus.CONNECTED`.

`test_connect_firebase_not_installed` tests the path where `shutil.which` returns `None`. This should produce a clear error rather than a subprocess crash, because trying to exec a missing binary would raise `FileNotFoundError` from deep in asyncio internals — a confusing error for the user.

`test_connect_not_logged_in` simulates the Firebase CLI returning an authentication error. The adapter must detect this and surface a human-readable status rather than propagating the raw CLI output.

## Execute Tests (`TestFirebaseAdapterExecute`)

The `_connected_adapter` helper patches subprocess and calls `connect()` to get an adapter in the connected state. Subsequent tests then call `execute()` with various action names:

- **`test_execute_not_connected`** — Calling `execute` before `connect` must return an error, not a crash. This prevents partially initialized adapters from producing confusing behavior.
- **`test_execute_unknown_action`** — An unrecognized action name returns an error dict. This is the connector's equivalent of HTTP 404.
- **`test_command_failure_returns_error`** — When the subprocess exits with a non-zero code, the adapter wraps the stderr into an error response rather than raising.
- **`test_command_timeout`** — The `slow_communicate` helper simulates a subprocess that never returns. The adapter must apply a timeout and return an error rather than hanging the event loop indefinitely.

## Sync and Schema Tests (`TestFirebaseAdapterSyncSchema`)

`test_sync_returns_not_supported` and `test_schema_returns_manual` confirm that the Firebase adapter does not support automatic schema sync (it uses the YAML definition as its schema). This is expected for CLI-wrapper connectors.

## Registry Tests (`TestFirebaseRegistry`)

Confirms that `firebase` appears in the CLI connectors registry and that `create_native_adapter` returns a `FirebaseAdapter` instance. This verifies the adapter is wired into PocketPaw's connector discovery system.

## Known Gaps

- **`_make_mock_proc` helper** — The mock subprocess helper is local to this test file. A shared fixture in `conftest.py` would avoid duplication across connector test files.
- **No test for concurrent execute calls** — Simultaneous executes on the same adapter instance are untested.
