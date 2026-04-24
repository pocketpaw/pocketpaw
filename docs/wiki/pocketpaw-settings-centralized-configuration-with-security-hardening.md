---
{
  "title": "PocketPaw Settings: Centralized Configuration with Security Hardening",
  "summary": "The `Settings` class is PocketPaw's single source of truth for all runtime configuration — agent backends, channel credentials, memory backends, security policies, soul protocol parameters, and user preferences. Built on `pydantic-settings`, it supports environment variable overrides, encrypted credential storage, SSRF-guarded URL fields, and automatic API key format validation.",
  "concepts": [
    "Settings",
    "pydantic-settings",
    "SSRF protection",
    "ExternalUrl",
    "API key validation",
    "CredentialStore",
    "file permissions",
    "agent backend",
    "soul protocol",
    "configuration"
  ],
  "categories": [
    "Configuration",
    "Security"
  ],
  "source_docs": [
    "5f480dc7423ee305"
  ],
  "backlinks": null,
  "word_count": 629,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/config.py` defines the `Settings` class and all supporting utility functions for PocketPaw's configuration system. This module is the most referenced file in the codebase — every subsystem that needs a setting imports from here. Its evolution history (visible in the module docstring) reflects the security hardening and feature additions made across multiple releases.

## pydantic-settings Foundation

`Settings` extends `BaseSettings` from `pydantic-settings`:

```python
model_config = SettingsConfigDict(
    env_prefix="POCKETPAW_",
    env_file=".env",
    extra="ignore",
)
```

This gives three configuration sources in priority order: environment variables (highest), `.env` file, and the JSON config file. The `env_prefix="POCKETPAW_"` means any `POCKETPAW_ANTHROPIC_API_KEY` environment variable overrides the stored config value — useful for CI, Docker, and secrets management systems.

`extra="ignore"` prevents Pydantic from raising validation errors when unknown fields are present in the config file (e.g., from a previous version's config). This makes `Settings` forward-compatible: old config files loaded by a newer version that has removed a field will not fail.

## SSRF Protection on URL Fields (ExternalUrl)

A key security addition (issue #703) introduced `ExternalUrl`:

```python
ExternalUrl = Annotated[str, AfterValidator(validate_external_url)]
```

Any settings field typed as `ExternalUrl` is validated on load by `validate_external_url`, which blocks loopback addresses and RFC1918 private ranges being used in outbound HTTP calls to external services. This prevents an SSRF (Server-Side Request Forgery) attack where a malicious config value could redirect the agent to query internal network services.

Fields using `ExternalUrl` include: `opencode_base_url`, `litellm_api_base`, `openai_compatible_base_url`, `mem0_ollama_base_url`, `embedding_base_url`, `signal_api_url`, and `mcp_client_metadata_url`.

## API Key Validation: Two-Layer Architecture

There are two separate validation functions with different intended callers:

- **`validate_api_key(field_name, value)`** — strict regex validation, returns `(bool, str)`. Used by the REST endpoint (`PUT /settings`) before saving a key. Provides targeted per-key error messages.
- **`validate_api_keys(settings)`** — loose prefix checks on a full `Settings` instance, returns `list[str]` warnings. Advisory only; callers must never block saves based on these results.

The two-layer design exists because the REST endpoint needs synchronous, targeted feedback before a write, while the batch validator is used for post-hoc advisory warnings (e.g., logged on `Settings.save()`).

## File Permissions Hardening

```python
def get_config_dir() -> Path:
    config_dir = Path.home() / ".pocketpaw"
    config_dir.mkdir(exist_ok=True)
    _chmod_safe(config_dir, 0o700)
    return config_dir
```

The config directory is created with mode `0o700` (owner read/write/execute only). Config files are set to `0o600` on save. This prevents other users on a shared system from reading API keys or session tokens. `_chmod_safe` wraps `chmod` in a try/except to silently ignore failures on Windows, which uses ACLs rather than POSIX permissions.

## Encrypted Credential Storage

As of 2026-02-06, secrets are stored encrypted via `CredentialStore`. `_migrate_plaintext_keys` performs a one-time migration of any plaintext API keys found in `config.json` to the encrypted store. This migration is idempotent — it only moves keys that have not yet been migrated — and runs transparently on first load after upgrade.

## Agent Backend and Fallback Chain

```python
agent_backend: str = Field(default="claude_agent_sdk", ...)
fallback_backends: list[str] = Field(default_factory=list, ...)
```

The primary backend defaults to `claude_agent_sdk` (the official Anthropic SDK). A `fallback_backends` list allows the agent to retry with alternative backends if the primary fails — useful for maintaining availability when an API provider is down.

## Soul Protocol Integration

The `soul_*` fields configure the soul-protocol integration: identity (`soul_name`, `soul_archetype`, `soul_persona`), OCEAN personality traits (`soul_ocean`), biorhythm dynamics (`soul_biorhythm`), and cognitive model for cheaper fact extraction (`soul_cognitive_model`). These fields are consumed by the soul engine at startup.

## Known Gaps

- **`soul_values` and `soul_ocean` not in dashboard UI**: The module docstring flags these with a `# TODO` — controls for these fields need to be added to a Soul settings tab when the dashboard UI is built out.
- **`extra="ignore"` silently drops unknown fields**: If a config file contains a misspelled field name, it will be silently ignored rather than flagged. An opt-in strict mode would help catch configuration mistakes during development.
