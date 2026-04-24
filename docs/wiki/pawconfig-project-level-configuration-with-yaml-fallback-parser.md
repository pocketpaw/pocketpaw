---
{
  "title": "PawConfig: Project-Level Configuration with YAML Fallback Parser",
  "summary": "PawConfig is a dataclass that loads paw.yaml from the project root, applies PAW_PROVIDER and PAW_SOUL_PATH environment variable overrides, and provides computed properties for the .paw directory and default soul file path. A minimal built-in YAML parser handles environments where PyYAML is not installed.",
  "concepts": [
    "PawConfig",
    "paw.yaml",
    "environment variables",
    "PyYAML fallback",
    "soul_path",
    "provider",
    "computed property",
    "project root"
  ],
  "categories": [
    "paw",
    "configuration"
  ],
  "source_docs": [
    "8c42f4fb32f5aeba"
  ],
  "backlinks": null,
  "word_count": 361,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`PawConfig` is the configuration object for a paw instance scoped to a specific project directory. It solves the problem of persisting per-project settings (soul name, LLM provider, soul file path) across `paw` invocations without requiring the user to re-specify them on every command.

## Load Hierarchy

Config values resolve in three layers from lowest to highest priority:

1. **Defaults** (`soul_name="Paw"`, `provider="claude"`)
2. **`paw.yaml`** in the project root
3. **Environment variables** (`PAW_PROVIDER`, `PAW_SOUL_PATH`)

```python
provider = os.environ.get("PAW_PROVIDER", data.get("provider", "claude"))
soul_path_str = os.environ.get("PAW_SOUL_PATH", data.get("soul_path"))
```

Environment variables win over `paw.yaml`, allowing CI pipelines and Docker containers to override settings without modifying the committed config file.

## PyYAML-Free Fallback

```python
def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        # Minimal key: value parser
        result = {}
        for line in f:
            ...
        return result
```

The fallback parser handles simple `key: value` lines, stripping quotes and treating YAML null values (`null`, `~`, empty string) as `None`. This ensures `paw.yaml` remains readable without PyYAML installed, which matters for minimal Docker images or embedded environments. The null-value handling uses a frozenset for O(1) membership testing:

```python
_YAML_NULL_VALUES: frozenset[str] = frozenset({"null", "~", ""})
```

## Computed Properties

`default_soul_path` and `paw_dir` are `@property` accessors rather than stored fields:

```python
@property
def default_soul_path(self) -> Path:
    return self.project_root / ".paw" / f"{self.soul_name.lower()}.soul"

@property
def paw_dir(self) -> Path:
    return self.project_root / ".paw"
```

This keeps the dataclass fields minimal and ensures changing `soul_name` after load automatically updates `default_soul_path`.

## soul_path vs. default_soul_path

`soul_path: Path | None = None` stores the explicit path from `paw.yaml` or env var. If `None`, callers fall back to `config.default_soul_path`. This lets users store their soul file in an arbitrary location (e.g., a shared network drive).

## Known Gaps

- **No validation**: `provider` accepts any string. An invalid value like `provider: gpt4` silently persists and fails later at agent construction.
- **Fallback parser does not handle multi-line values**: `paw.yaml` values with colons (e.g., `soul_path: /home/user/my:path`) would be mis-parsed. PyYAML handles this correctly.
- **No config write-back**: `PawConfig.load()` reads but there is no `save()` method. `paw init` writes `paw.yaml` manually via string formatting, bypassing the dataclass.