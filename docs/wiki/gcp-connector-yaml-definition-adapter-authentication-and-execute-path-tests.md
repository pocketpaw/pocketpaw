---
{
  "title": "GCP Connector: YAML Definition, Adapter Authentication, and Execute Path Tests",
  "summary": "This test suite validates PocketPaw's Google Cloud Platform connector, covering the `gcp.yaml` declarative definition (20 actions across compute, storage, pub/sub, secrets, Cloud Run, and logging), the `GCPAdapter` lifecycle (connect, execute, disconnect), and registry wiring. All subprocess calls are mocked to avoid requiring live GCP credentials.",
  "concepts": [
    "GCP connector",
    "gcp.yaml",
    "GCPAdapter",
    "gcloud CLI",
    "async subprocess",
    "trust levels",
    "required params",
    "storage",
    "pub/sub",
    "Cloud Run",
    "secret manager",
    "connector registry"
  ],
  "categories": [
    "testing",
    "connectors",
    "Google Cloud Platform",
    "CLI integration",
    "test"
  ],
  "source_docs": [
    "3d29c0fa1477391b"
  ],
  "backlinks": null,
  "word_count": 503,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's GCP connector wraps the `gcloud` CLI binary to give AI agents read and write access to Google Cloud resources. Like the Firebase connector, it uses async subprocess execution rather than the GCP Python SDK, keeping the dependency footprint minimal and supporting any gcloud-authenticated environment.

## YAML Definition Tests (`TestYAMLParsing`)

The `gcp.yaml` file declares 20 actions spanning major GCP services:

- **Storage**: `storage_list_buckets`, `storage_list_objects`, `storage_get_object`, `storage_copy`, `storage_delete`
- **Pub/Sub**: `pubsub_list_topics`, `pubsub_list_subscriptions`, `pubsub_publish`
- **Cloud Run**: `run_list_services`, `run_describe_service`, `run_list_revisions`
- **Secret Manager**: `secrets_list`, `secrets_get`, `secrets_create`
- **Logging**: `logs_read`
- **Compute**: `compute_list_instances`
- **Project**: `list_projects`, `get_project`

**Auth method is `none`** — authentication uses the gcloud CLI's existing credential session (`gcloud auth login` or service account key). GCP_PROJECT and GCP_REGION are optional environment hints, not secrets.

**Trust levels** follow the same destructive-action pattern as Firebase: read operations are `auto`, writes and deletes require higher trust levels.

**`test_required_params`** ensures that actions with mandatory parameters (e.g., `get_project` requires a project ID, `storage_get_object` requires bucket and object path) mark those params as required in their schema. This prevents agents from calling actions with missing arguments that would cause cryptic CLI errors.

## Adapter Connect Tests (`TestAdapterConnect`)

- **`test_connect_success`** — Patches `gcloud projects list` to return a valid project list. Verifies `ConnectorStatus.CONNECTED`.
- **`test_connect_with_project`** — When a `GCP_PROJECT` credential is supplied, the adapter connects with a specific project context.
- **`test_connect_no_gcloud`** — `gcloud` binary not found produces a user-readable error, not an `OSError`.
- **`test_connect_not_authenticated`** — gcloud returning an auth error produces a specific status, not a generic failure.
- **`test_connect_gcloud_error`** — Any non-zero gcloud exit code during connect is handled gracefully.

## Execute Tests (`TestAdapterExecute`)

The `_connect_adapter` helper mocks gcloud to return a successful project list, then calls `connect()` to get an adapter in the connected state.

- **`test_execute_not_connected`** — Calls `execute` on a fresh (not connected) adapter. Must return an error, not raise.
- **`test_execute_unknown_action`** — Unknown action names return an error dict.
- **`test_list_projects`** — Verifies the gcloud command is assembled correctly and the output is parsed.
- **`test_get_project_missing_param`** — Missing required param returns a validation error before the subprocess is even called. This avoids a confusing gcloud CLI error reaching the user.
- **`test_storage_get_object_missing_params`** — Same pattern for multi-parameter actions.
- **`test_gcloud_command_failure`** — Non-zero exit code from gcloud returns an error response.
- **`test_empty_output`** — gcloud returning empty stdout (common for listing operations on empty projects) must not crash the JSON parser.

## Actions Tests (`TestAdapterActions`)

`test_actions_from_yaml` verifies the adapter's `actions()` method returns schemas derived from the YAML definition. `test_actions_fallback_without_yaml` verifies the adapter gracefully handles the case where no YAML is available (returns an empty list rather than raising).

## Registry Tests (`TestRegistryIntegration`)

Confirms `gcp` appears in the CLI connectors registry and `create_native_adapter` returns a `GCPAdapter`. This is the same wiring test pattern used across all connectors.

## Known Gaps

The test count comment says "21 actions total" but asserts `len(gcp_definition.actions) == 20`. This discrepancy is noted in the test (`test_action_count`) and may indicate a planned action that was removed or not yet added.
