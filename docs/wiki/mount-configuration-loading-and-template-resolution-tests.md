---
{
  "title": "Mount Configuration Loading and Template Resolution Tests",
  "summary": "This module tests the `load_mounts` and `resolve_template` functions that parse and validate `mounts.yaml` configuration files, covering sort order, variable substitution, unknown-variable rejection, and absolute-path enforcement. These tests prevent misconfigured mount definitions from silently producing incorrect or insecure virtual filesystem paths.",
  "concepts": [
    "load_mounts",
    "resolve_template",
    "MountConfig",
    "mounts.yaml",
    "mount ordering",
    "variable substitution",
    "absolute path validation",
    "virtual filesystem",
    "cloud files configuration"
  ],
  "categories": [
    "Cloud Files",
    "Testing",
    "Configuration",
    "Virtual Filesystem",
    "test"
  ],
  "source_docs": [
    "60566bfa58d4c6d2"
  ],
  "backlinks": null,
  "word_count": 514,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/cloud/files/test_mounts_config.py` covers two functions from `ee.cloud.files.mounts_config`:

- **`load_mounts(path)`**: reads a `mounts.yaml` file, validates each entry against the `MountConfig` schema, and returns the list sorted by `order`.
- **`resolve_template(template, variables)`**: interpolates `{placeholder}` tokens in a mount template string using a provided variables dict.

These functions are the gateway through which deployment-time configuration enters the runtime. A bug here can produce wrong mount paths, expose wrong providers, or silently skip validation, so the tests cover both the happy path and several failure modes.

## Test Breakdown

### `test_load_mounts_returns_ordered_list`

Two mount entries are written to a temporary YAML file with `order: 20` and `order: 10` respectively. The test asserts that `load_mounts` returns them sorted ascending by `order`, with `b` (order 10) before `a` (order 20).

```python
cfg = load_mounts(yaml)
assert [m.provider_id for m in cfg] == ["b", "a"]
assert all(isinstance(m, MountConfig) for m in cfg)
```

The `order` field controls how mounts appear in the virtual folder tree and how longest-prefix resolution breaks ties. If YAML parse order were used instead, the tree would be non-deterministic across YAML editors and file merges.

### `test_resolve_template_substitutes_vars`

Verifies that a template like `/Workspaces/{workspace_id}/KB` with `{"workspace_id": "ws_1"}` produces `/Workspaces/ws_1/KB`. This is the core mechanism by which per-user or per-workspace mounts are materialized at request time.

### `test_resolve_template_leaves_unknown_vars_as_error`

When a required variable is missing from the dict, `resolve_template` must raise `KeyError`. The alternative -- leaving the `{placeholder}` literal in the path -- would cause downstream path resolution to produce incorrect mounts silently. Raising immediately forces callers to supply all required variables.

```python
with pytest.raises(KeyError):
    resolve_template("/Workspaces/{workspace_id}/KB", {})
```

### `test_resolve_template_no_vars`

A template with no placeholders (e.g., `/My Files`) must pass through unchanged when given an empty variables dict. This confirms the implementation does not error on clean templates, which is the common case for fixed mounts like the personal uploads folder.

### `test_load_mounts_rejects_relative_template`

If a `mount_template` in YAML is a relative path (e.g., `relative/path` instead of `/relative/path`), `load_mounts` must raise `ValueError`. This is a security-relevant check: relative mount paths could resolve to ambiguous locations depending on the caller's working directory, leading to path traversal risks or mounting against unintended filesystem locations.

```python
yaml.write_text(
    "- provider_id: a
  mount_template: relative/path
  writable: false
  order: 1
"
)
with pytest.raises(ValueError):
    load_mounts(yaml)
```

## Why This Validation Matters

Mount templates form the root of the virtual filesystem namespace. A misconfigured template can:

1. **Produce duplicate mounts** if two providers share the same effective path after variable substitution.
2. **Break longest-prefix resolution** if order is wrong, routing requests to the wrong provider.
3. **Enable path traversal** if relative paths are permitted and the caller's working directory is attacker-controlled.

By validating at load time, errors surface in configuration review rather than at the moment a user tries to browse a file.

## Known Gaps

There are no tests for malformed YAML (e.g., missing required fields like `provider_id`). Pydantic's `MountConfig` schema validation would catch this, but the test does not explicitly exercise that path. Additionally, there is no test for duplicate `provider_id` entries within a single YAML file, which may or may not be rejected by the current implementation.
