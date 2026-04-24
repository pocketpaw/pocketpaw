---
{
  "title": "MongoDB Cloud Database Diagnostic: Collection Counts and Sample Document Inspection",
  "summary": "A targeted diagnostic script for the PocketPaw enterprise cloud MongoDB database that lists collection document counts and prints sample documents from key collections. Unlike the broader `diag_mongo_all.py`, this script focuses on the `paw-cloud` database and respects the `POCKETPAW_CLOUD_MONGO_URI` environment variable for flexible targeting.",
  "concepts": [
    "MongoDB",
    "Motor",
    "paw-cloud",
    "POCKETPAW_CLOUD_MONGO_URI",
    "cloud database",
    "session inspection",
    "message inspection",
    "enterprise backend",
    "raw Motor client",
    "diagnostic sampling",
    "password exclusion"
  ],
  "categories": [
    "scripts",
    "diagnostics",
    "database",
    "enterprise"
  ],
  "source_docs": [
    "bc479705b1512eae"
  ],
  "backlinks": null,
  "word_count": 426,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`scripts/diag_mongo_state.py` is a more focused diagnostic than `diag_mongo_all.py`. Where the latter answers "where is data?", this script answers "what is in the cloud database right now?" It is targeted at the enterprise cloud backend's `paw-cloud` database and is the go-to script when debugging cloud persistence issues like sessions not being created, messages not being saved, or users not being found.

## Environment Variable Support

The script reads the database URI from `POCKETPAW_CLOUD_MONGO_URI`, defaulting to `mongodb://localhost:27017/paw-cloud`. This makes it useful in multiple contexts:

```bash
# Local dev (default)
uv run python scripts/diag_mongo_state.py

# Custom local database
POCKETPAW_CLOUD_MONGO_URI=mongodb://localhost:27017/paw-cloud uv run python scripts/diag_mongo_state.py

# Staging environment (if network-accessible)
POCKETPAW_CLOUD_MONGO_URI=mongodb://staging-host:27017/paw-cloud uv run python scripts/diag_mongo_state.py
```

The database name is parsed from the URI by splitting on `/` and `?` — this handles URIs with query parameters (e.g., `?authSource=admin`) correctly.

## What It Prints

### Collection Overview
First, all collections in the target database are listed with their total document counts. This quickly shows whether the database has been initialized at all, and whether any collections are unexpectedly empty.

### Sample Documents
For five key collections — `sessions`, `messages`, `users`, `workspaces`, `groups` — the script prints the first three documents. This is useful for verifying document structure, checking that required fields are present, and spotting obviously wrong values (e.g., null user IDs, missing timestamps).

Passwords are explicitly excluded from the sample output:
```python
doc_out = {k: v for k, v in doc.items() if k != "password"}
```
This is a defensive measure: running the diagnostic in a shared terminal session should not expose credentials in the scrollback buffer.

## Why Sample Size Is 3

Three documents is enough to spot structural inconsistencies (e.g., one document is missing a field that the others have) while keeping the output readable. It is not intended as a full data dump.

## Design Pattern

Like `diag_mongo_all.py`, `motor` is imported inside `main()`. The script uses a direct `AsyncIOMotorClient` rather than going through PocketPaw's Beanie ODM layer — this is intentional. Beanie validation might reject documents that have schema issues, masking the very problem being diagnosed. The raw Motor client shows exactly what is in the database, regardless of whether it passes model validation.

## Known Gaps

- Only five hardcoded collection names are sampled; new collections added to the enterprise model are not automatically included.
- The script does not show index information, which is often relevant when diagnosing slow queries or missing constraints.
- The password exclusion only filters a literal `"password"` key; field names like `hashed_password` or `password_hash` would still be shown.