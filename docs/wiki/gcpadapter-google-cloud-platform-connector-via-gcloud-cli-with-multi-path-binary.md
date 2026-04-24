---
{
  "title": "GCPAdapter: Google Cloud Platform Connector via gcloud CLI with Multi-Path Binary Discovery",
  "summary": "`GCPAdapter` implements `ConnectorProtocol` by wrapping the `gcloud` CLI, using `--format=json` for structured output and supporting project and region overrides via config. It scans several common install paths to locate the binary, making it resilient to non-standard SDK installations.",
  "concepts": [
    "GCPAdapter",
    "gcloud CLI",
    "Application Default Credentials",
    "binary discovery",
    "ConnectorProtocol",
    "asyncio subprocess",
    "Cloud Run",
    "BigQuery",
    "GKE",
    "project scoping"
  ],
  "categories": [
    "connectors",
    "Google Cloud Platform",
    "infrastructure"
  ],
  "source_docs": [
    "3c6e581a394eebee"
  ],
  "backlinks": null,
  "word_count": 449,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`GCPAdapter` is a native Python connector for Google Cloud Platform. Like `FirebaseAdapter`, it avoids REST API complexity by shelling out to the `gcloud` CLI and parsing JSON output. This lets PocketPaw expose GCP operations (Compute, GKE, Cloud Run, Storage, Pub/Sub, IAM, BigQuery, and more) without managing service account credentials or OAuth flows internally.

## Binary Discovery

```python
_GCLOUD_PATHS = [
    "/opt/homebrew/share/google-cloud-sdk/bin/gcloud",
    "/usr/local/bin/gcloud",
    "/usr/bin/gcloud",
    "/snap/bin/gcloud",
]

def _find_gcloud() -> str | None:
    found = shutil.which("gcloud")  # PATH first
    if found:
        return found
    for p in _GCLOUD_PATHS:         # then common locations
        if Path(p).is_file():
            return p
    return None
```

`shutil.which` checks PATH first (covers most developer setups), then falls back to a hardcoded list of common install locations (Homebrew on macOS, snap on Ubuntu, standard `/usr/local` on Linux). This is important because `gcloud` is often installed via the Google Cloud SDK installer into a non-standard path that does not appear in PATH inside some environments.

## Authentication Check on Connect

`connect()` immediately runs `gcloud auth list --format=json` and looks for an account with `"status": "ACTIVE"`. If no active account exists, it returns a connection error with the correct remediation command (`gcloud auth login`). This fail-fast check prevents silent failures later when an unauthenticated command would time out or return an opaque error.

## `_run_gcloud` — Shared Execution Primitive

Similar to `FirebaseAdapter._run_cmd`, all actions route through a single async subprocess helper. It appends `--format=json` and optional `--project` / `--region` flags from the stored config. The consistent interface means adding new GCP subcommands is a matter of mapping an action name to a `gcloud` subcommand list.

## Action Surface

The adapter exposes operations across multiple GCP product areas via `gcloud` subcommands:

- **Compute Engine**: list instances, describe, start/stop
- **GKE**: list clusters, get credentials
- **Cloud Run**: list services, describe, deploy
- **Cloud Storage**: list buckets, upload/download objects
- **Pub/Sub**: list topics and subscriptions, publish
- **IAM**: list service accounts, roles
- **BigQuery**: list datasets and tables, run queries
- **Cloud SQL**: list instances
- **Secret Manager**: list secrets, access versions

## Project and Region Scoping

The adapter stores `GCP_PROJECT` and `GCP_REGION` from the config dict at connect time and appends them to relevant commands automatically. This prevents actions from accidentally running against the wrong GCP project when the `gcloud` default project differs from the intended one.

## Known Gaps

- **No streaming for long-running operations** (e.g., deploy, BigQuery query): commands time out at 30 seconds — operations exceeding this window silently fail.
- **No service account key auth**: relies entirely on the gcloud ADC (Application Default Credentials) session; service account JSON key files are not supported by the adapter.
- **`sync()` is a stub**: no data is pulled into Single Brain.
