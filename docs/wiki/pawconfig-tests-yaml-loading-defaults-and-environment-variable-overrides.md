---
{
  "title": "PawConfig Tests: YAML Loading, Defaults, and Environment Variable Overrides",
  "summary": "`PawConfig` is PocketPaw's project-level configuration object, loaded from `paw.yaml` in the project root. These tests cover the full priority chain: defaults when no file exists, YAML values when the file is present, and environment variable overrides that take precedence over both.",
  "concepts": [
    "PawConfig",
    "paw.yaml",
    "configuration loading",
    "environment variable overrides",
    "PAW_PROVIDER",
    "PAW_SOUL_PATH",
    "soul_name",
    "paw_dir",
    "twelve-factor config",
    "monkeypatch"
  ],
  "categories": [
    "testing",
    "configuration",
    "agent runtime",
    "test"
  ],
  "source_docs": [
    "857fcc6a6818c020"
  ],
  "backlinks": null,
  "word_count": 444,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`PawConfig.load()` is called at agent startup to determine the soul name, LLM provider, and soul file path. Getting this wrong means the agent boots with the wrong identity or fails to find its soul. The test suite validates every layer of the configuration stack.

## Default Behavior (No paw.yaml)

`TestPawConfigDefaults` covers the cold-start case where no configuration file exists:

- **Defaults returned**: `load()` returns a `PawConfig` with sensible defaults rather than raising `FileNotFoundError`.
- **CWD fallback**: when no `root` argument is passed, `load()` uses the current working directory. The monkeypatch ensures this is deterministic in tests.
- **Default soul path**: derived from the soul name, preventing the path from being hardcoded to a development machine location.
- **`.paw` directory path**: always set to `.paw/` under the project root, not relative to the current working directory of the calling process.

```python
def test_default_soul_path_uses_soul_name(tmp_path):
    config = PawConfig.load(root=tmp_path)
    assert config.soul_name.lower() in str(config.soul_path)
```

## YAML Loading

`TestPawConfigFromYaml` writes a minimal `paw.yaml` to `tmp_path` and verifies each field is parsed correctly:

- `soul_name`, `provider`, and `soul_path` are loaded from their respective YAML keys.
- `name` is an alias for `soul_name` — tested separately to confirm backward compatibility with older config files.
- An empty YAML file (`{}`) falls back to defaults rather than raising a `KeyError`.

The alias test (`test_name_alias_for_soul_name`) is particularly important because early PocketPaw projects used `name:` instead of `soul_name:`, and changing the key without supporting the alias would break existing setups silently.

## Environment Variable Overrides

`TestPawConfigEnvOverrides` tests `PAW_PROVIDER` and `PAW_SOUL_PATH` environment variables:

- Each env var overrides both the YAML value and the default.
- Both variables are tested in isolation to confirm there's no cross-contamination.

```python
def test_paw_provider_overrides_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("PAW_PROVIDER", "ollama")
    config = PawConfig.load(root=tmp_path)
    assert config.provider == "ollama"
```

Environment variable overrides exist so that CI/CD pipelines and Docker containers can configure the agent without modifying the committed `paw.yaml` file — a standard twelve-factor app pattern.

## Property Correctness

`TestPawConfigProperties` covers two subtle behaviors:

- **Lowercase soul name in path**: `test_default_soul_path_lowercases_soul_name` ensures the soul file path doesn't contain uppercase letters that would cause case-sensitive filesystem mismatches on Linux.
- **Non-creating directory**: `test_paw_dir_does_not_create_directory` verifies that accessing the `paw_dir` property does not create the `.paw/` directory as a side effect. This matters because the property may be accessed for display purposes before the project is initialized.

## Known Gaps

- No test covers what happens if `paw.yaml` contains invalid YAML syntax (parser error path).
- `PAW_SOUL_PATH` override is tested but no test verifies the path is resolved relative to a consistent base (absolute vs. relative paths).
- No test for a `paw.yaml` with unknown/extra keys — silent ignore vs. warning behavior is unspecified.