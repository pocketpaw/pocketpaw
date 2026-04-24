---
{
  "title": "FirebaseAdapter: CLI-Backed Connector for Firestore, Auth, Hosting, Functions, and Remote Config",
  "summary": "`FirebaseAdapter` implements the `ConnectorProtocol` by shelling out to the `firebase` CLI with `--json --non-interactive` flags rather than making REST calls directly, delegating authentication entirely to the Firebase CLI's own session. It supports a wide surface: Firestore CRUD, Auth user management, Hosting deploys, Cloud Functions, Remote Config, and Extensions.",
  "concepts": [
    "FirebaseAdapter",
    "firebase CLI",
    "asyncio.create_subprocess_exec",
    "Firestore",
    "Firebase Auth",
    "Firebase Hosting",
    "Cloud Functions",
    "Remote Config",
    "ConnectorProtocol",
    "shell-out adapter"
  ],
  "categories": [
    "connectors",
    "Firebase",
    "Google Cloud"
  ],
  "source_docs": [
    "7d1042ebf6889a72"
  ],
  "backlinks": null,
  "word_count": 465,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`FirebaseAdapter` is a native Python connector adapter that wraps the `firebase-tools` CLI. The design choice to shell out rather than use a REST SDK avoids managing OAuth token refresh, scoping, and client library versions inside PocketPaw — the CLI handles all of that transparently once the user has run `firebase login`.

## Why CLI-Backed Instead of REST?

Firebase's REST API requires service account credentials or OAuth tokens with specific scopes per API surface (Firestore, Hosting, Functions each have separate auth domains). The `firebase` CLI, by contrast, handles multi-service auth in a single session. Shelling out trades REST efficiency for zero credential-management complexity inside the adapter.

## `_run_cmd` — The Core Primitive

```python
async def _run_cmd(self, *args: str, timeout: float = 30) -> tuple[bool, Any]:
```

Every action dispatches through `_run_cmd`, which:

1. Appends `--json` and `--non-interactive` to every command.
2. Adds `--project <id>` when a project is configured.
3. Waits for output with `asyncio.wait_for` (30-second timeout per command).
4. Parses JSON from stdout — Firebase wraps results in `{"status": "success", "result": ...}`.
5. On non-zero exit, attempts to extract the error message from the JSON payload first, then falls back to stderr text.

The `--non-interactive` flag prevents the CLI from pausing to prompt the user — critical for agent contexts where there is no terminal to interact with.

## Error Handling Strategy

The function returns `(bool, Any)` rather than raising, so every action method gets a consistent `success, data` pair. This keeps action implementations simple and ensures that a bad subcommand (e.g., wrong project name) surfaces as a structured `ActionResult(success=False, ...)` rather than an unhandled exception in the router.

Specific failure modes handled:
- `TimeoutError` — returns a user-readable timeout message
- `FileNotFoundError` — means the `firebase` binary is not on PATH; returns install instructions
- Non-zero exit code — extracts the structured error from the JSON envelope if available

## Supported Actions

| Category | Actions |
|---|---|
| Projects | `list_projects`, `get_project` |
| Firestore | `list_collections`, `list_databases`, `get_document`, `delete_document`, `export_data` |
| Auth | `list_users`, `import_users` |
| Hosting | `list_sites`, `deploy` |
| Functions | `list_functions`, `get_logs`, `deploy` |
| Remote Config | `get_config` |
| Extensions | `list_extensions` |

## Configuration

The adapter accepts `FIREBASE_CLI_PATH` in the `config` dict passed to `connect()` to override the binary location. This enables use in environments where `firebase` is not on the system PATH (e.g., project-local `node_modules/.bin/firebase`).

## Known Gaps

- **No ingest/sync implementation**: `sync()` returns a `SyncResult` stub. Firestore data is not pulled into Single Brain.
- **No streaming for large exports**: `_firestore_export` initiates a GCS export but does not stream progress back.
- **CLI auth coupling**: If the Firebase CLI session expires, the adapter returns an error with no automatic re-auth path — the user must run `firebase login` manually.
