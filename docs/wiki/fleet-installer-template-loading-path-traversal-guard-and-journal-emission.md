---
{
  "title": "Fleet Installer — Template Loading, Path Traversal Guard, and Journal Emission",
  "summary": "The Fleet installer loads YAML or JSON fleet templates, installs the bundled soul/pocket/connector/scope configuration, and emits correlated journal events for observability. A critical path-traversal security fix was applied in `fix/fleet-install-auth-guard`: string-path inputs from the REST router are now clamped to the bundled templates directory, preventing a workspace admin from coercing the server into reading arbitrary files.",
  "concepts": [
    "install_fleet",
    "load_fleet",
    "path traversal",
    "is_relative_to",
    "FleetInstallReport",
    "FleetInstallStep",
    "journal emission",
    "correlation_id",
    "YAML manifest",
    "trusted path",
    "optional connector",
    "scope resolution"
  ],
  "categories": [
    "fleet",
    "security",
    "installation",
    "journal",
    "orchestration"
  ],
  "source_docs": [
    "e43f5809fc35c69f"
  ],
  "backlinks": null,
  "word_count": 486,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Core Responsibility

`install_fleet` is pure orchestration: it calls into `SoulFactory`, `ConnectorRegistry`, and the Pocket service in sequence, wraps each step in a `FleetInstallStep`, and returns a `FleetInstallReport` with per-step status. This design means partial failures are observable — the UI can show exactly which steps succeeded and which failed without re-running the whole install.

## Path Traversal Fix

The most significant security concern in this file is the `load_fleet` function. Before `fix/fleet-install-auth-guard`, `load_fleet` fell through to `Path(path_or_name)` for any string that did not match a bundled template, letting a workspace admin pass `"../../etc/passwd"` and have the server read arbitrary files.

The fix clamps all string inputs to `_BUNDLED_DIR`:

```python
bundled_dir = _BUNDLED_DIR.resolve()
candidate = (bundled_dir / f"{path_or_name}.yaml").resolve()
if not candidate.is_relative_to(bundled_dir) or not candidate.exists():
    raise FileNotFoundError(f"Fleet template not found: {path_or_name}")
```

`is_relative_to` catches both `..` traversal and absolute paths (e.g., `/etc/passwd` resolves outside `bundled_dir`). The error message never echoes the attempted filesystem path, preventing directory layout leakage. `Path` instances bypass the clamp because they only come from trusted programmatic callers (tests, scripts) — the REST router only ever passes strings.

## Dual-Path Trust Model

The split between string inputs (clamped, untrusted) and `Path` inputs (unclamped, trusted) is a deliberate API design: the type of the argument encodes the trust level. This avoids a flag parameter (`trusted=True`) that callers might pass incorrectly.

## Journal Emission

Added in `feat/fleet-journal-emission`, `install_fleet` accepts an optional `journal` and `actor`. When provided, it emits a correlated trio:

1. `fleet.install.started` — extension namespace event at install start.
2. `agent.spawned` — canonical event per soul created.
3. `fleet.installed` — summary event at completion.

All three share a `correlation_id` generated at install start, so operators can trace a single install across the event log. Emission errors are logged and swallowed — the journal is observability infrastructure, not control flow. An emit failure must not roll back a successful install.

## Scope Resolution

`_resolve_scope` picks the scope list for journal events from the fleet template's scopes. When no scopes are defined, it falls back to a system-level scope so the event is always valid (the journal's `EventEntry` invariant requires a non-empty scope).

## YAML Dependency

Fleet templates are YAML files. `PyYAML` is an optional dependency pulled in via the `pocketpaw[soul]` extra. If a caller tries to load a YAML template without PyYAML installed, the import error is caught and re-raised with a clear message pointing at the correct install command (`pip install pocketpaw[soul]`).

## Known Gaps

- The `install_fleet` source was truncated in the extract; the full orchestration body (SoulFactory call, ConnectorRegistry call, Pocket creation) is not shown. The architecture is well-documented in comments but the step sequence should be verified against the full file before extending.
- Connector registration uses `optional: bool` on each `FleetConnector` to allow silent skip if the module is missing. This means connector failures are not always surfaced in the report — operators should check `failed_steps()` and also inspect `skipped` steps.