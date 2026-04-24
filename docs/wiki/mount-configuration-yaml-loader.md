---
{
  "title": "Mount Configuration YAML Loader",
  "summary": "Provides `load_mounts`, which reads a `mounts.yaml` file, validates each row into a typed `MountConfig` object, and returns them sorted by `order`. Also exposes `resolve_template` for expanding path templates containing workspace or user variables at request time.",
  "concepts": [
    "load_mounts",
    "resolve_template",
    "MountConfig",
    "mounts.yaml",
    "YAML loader",
    "yaml.safe_load",
    "path templates",
    "mount ordering",
    "ProviderRegistry",
    "startup validation"
  ],
  "categories": [
    "files",
    "configuration",
    "cloud",
    "mounts"
  ],
  "source_docs": [
    "20e9b0d5f424429d"
  ],
  "backlinks": null,
  "word_count": 460,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `ee.cloud.files.mounts_config` module is the bridge between static mount definitions stored in `mounts.yaml` and the typed `MountConfig` objects the `ProviderRegistry` consumes. It intentionally keeps I/O and validation together in a thin loader rather than scattering `yaml.safe_load` calls across the codebase.

## load_mounts

```python
def load_mounts(path: Path | None = None) -> list[MountConfig]:
    src = path or _DEFAULT_PATH
    raw = yaml.safe_load(src.read_text()) or []
    configs = [MountConfig(**row) for row in raw]
    configs.sort(key=lambda c: c.order)
    return configs
```

`load_mounts` uses `yaml.safe_load` (never `yaml.load`) to prevent arbitrary Python object deserialisation -- a key supply-chain safety measure. Each raw dict is immediately validated by `MountConfig(**row)`, which is a Pydantic model. Any missing required field or type mismatch raises a `ValidationError` at startup rather than silently producing a malformed mount at request time.

The `or []` guard handles an empty YAML file gracefully -- `yaml.safe_load` returns `None` for empty input, which would crash the list comprehension without the fallback.

Sorting by `c.order` ensures that `ProviderRegistry.resolve_mount` performs longest-prefix matching in a deterministic order. Without sorting, the order of mounts in YAML would affect routing, creating a fragile implicit dependency on file layout.

The `_DEFAULT_PATH` constant resolves to `mounts.yaml` in the same directory as the module, so the loader works without configuration in both development and production. The optional `path` override exists for tests, which can pass a temporary file without monkey-patching.

## resolve_template

```python
def resolve_template(template: str, variables: dict[str, str]) -> str:
    return template.format(**variables)
```

Mount paths in `mounts.yaml` may contain placeholders such as `"/workspaces/{workspace_id}/files"`. `resolve_template` uses Python's built-in `str.format` to expand those placeholders at request time when the registry calls `ResolvedMount` construction.

Using `str.format` rather than a custom parser keeps the implementation minimal. The downside is that a template with an unknown placeholder raises a `KeyError` at request time rather than at load time. The registry is expected to pass complete `variables` dicts; missing keys surface as 500 errors rather than validation failures at startup.

## Why YAML over a Database?

Mount definitions are deployment-level configuration: they describe which providers exist and what paths they serve. This is analogous to nginx location blocks -- it changes when the service topology changes, not when users interact with files. Storing them in YAML keeps them in version control, reviewable in PRs, and deployable via config map without a database migration.

## Known Gaps

- **No hot-reload.** `load_mounts` reads the file once at startup. Adding or removing mounts requires a service restart.
- **Template injection risk.** `str.format` with user-controlled variables would allow key injection (`{__class__}` style attacks). The current call sites pass only system-controlled variables (`workspace_id`, `user_id`), but this is a convention, not a type-level enforcement.
- **No schema validation of mounts.yaml itself.** Invalid YAML structure raises a confusing Pydantic error rather than a clear configuration error.