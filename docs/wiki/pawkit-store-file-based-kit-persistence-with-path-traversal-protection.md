---
{
  "title": "PawKit Store: File-Based Kit Persistence with Path Traversal Protection",
  "summary": "The `FileKitStore` manages installed PawKits on disk under `~/.pocketpaw/kits/`, using atomic writes (write-then-rename) and an in-memory index loaded lazily on first access. A March 2026 security hardening pass added input sanitization to prevent path traversal attacks via malicious kit IDs or data source names.",
  "concepts": [
    "FileKitStore",
    "atomic writes",
    "path traversal",
    "slug sanitization",
    "singleton pattern",
    "in-memory index",
    "YAML persistence",
    "workflow data",
    "reset_kit_store",
    "security hardening"
  ],
  "categories": [
    "kits",
    "storage",
    "security"
  ],
  "source_docs": [
    "ca8b34275dd7d7f6"
  ],
  "backlinks": null,
  "word_count": 377,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/kits/store.py` is the persistence layer for installed PawKits. It writes kit configurations as YAML files and workflow output as JSON, using a directory-per-kit layout under `~/.pocketpaw/kits/`.

## Storage Layout

```
~/.pocketpaw/kits/
    mission-control/
        pawkit.yaml    — parsed into PawKitConfig
        data/
            agent-fleet.json   — workflow output
    deep-work/
        pawkit.yaml
        data/
```

Each installed kit gets its own directory, named by a slugified kit ID. Workflow data files are stored under `data/`, keyed by the source name.

## Atomic Writes

All writes use the write-to-temp-then-rename pattern:

```python
tmp = path.with_suffix(".tmp")
tmp.write_text(content)
tmp.rename(path)
```

This prevents partial writes from leaving a corrupt file. If the process crashes mid-write, only the `.tmp` file is corrupted — the original file remains intact. Without this, a crash during a YAML write could make the kit unloadable.

## Lazy In-Memory Index

The store maintains an in-memory dict of `{kit_id: InstalledKit}`, populated on first access via `_ensure_loaded()`. This avoids scanning the kits directory on every import while keeping reads fast after the first call.

## Singleton Factory

```python
def get_kit_store(base_dir: Path | None = None) -> FileKitStore: ...
def reset_kit_store() -> None: ...
```

`reset_kit_store()` exists specifically for testing — it clears the singleton so each test can start with a fresh, isolated store without process restart.

## Security Hardening: Path Traversal Prevention

Updated March 2026, the store now sanitizes all kit IDs and data source names before constructing file paths:

```python
def _slugify(name: str) -> str:
    # Strips anything that is not alphanumeric, dash, or underscore
    ...
```

Without this, a kit ID of `../../etc/passwd` or a data source name containing `../` could escape the `~/.pocketpaw/kits/` directory and write arbitrary files. The sanitization rejects path separators and control characters before they reach `pathlib`.

## CRUD Operations

- `install_kit(yaml_str, kit_id)` — parses YAML, writes `pawkit.yaml`, updates index
- `list_kits()` — returns all installed kits from the in-memory index
- `remove_kit(kit_id)` — deletes the kit directory, updates index
- `activate_kit(kit_id)` — toggles `InstalledKit.active`, re-saves YAML
- `save_kit_data(kit_id, source, data)` — atomically writes workflow output JSON

## Known Gaps

- **No concurrent-write protection**: Multiple processes writing to the same kit simultaneously could race on the temp-rename pattern.
- **YAML parsed with custom logic**: `_parse_yaml_string()` is hand-rolled rather than using PyYAML, which may not handle all valid YAML edge cases.