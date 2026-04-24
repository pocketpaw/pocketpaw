---
{
  "title": "API Key Health Checks: Presence, Format Validation, and Encryption Verification",
  "summary": "The `api_keys.py` module implements four health checks covering the full lifecycle of API credentials: whether a key exists for the selected backend, whether it matches the expected format prefix, whether required backend packages are installed, and whether the secrets file is Fernet-encrypted rather than plaintext. Together these checks prevent the most common misconfiguration failures that cause silent agent startup errors.",
  "concepts": [
    "API key validation",
    "Fernet encryption",
    "check_api_key_primary",
    "check_secrets_encrypted",
    "backend dependencies",
    "key format patterns",
    "per-backend validation",
    "HealthCheckResult",
    "legacy backend migration"
  ],
  "categories": [
    "health monitoring",
    "security",
    "configuration",
    "diagnostics"
  ],
  "source_docs": [
    "98411b390d90400a"
  ],
  "backlinks": null,
  "word_count": 468,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`api_keys.py` (`src/pocketpaw/health/checks/api_keys.py`) is the most backend-aware health check module, containing per-provider key validation logic for Claude SDK, Google ADK, and OpenAI Agents backends.

## check_api_key_primary

This check dispatches to a per-backend helper based on `settings.agent_backend`:

```python
if backend == "claude_agent_sdk":
    return _check_claude_sdk_key(settings)
elif backend == "google_adk":
    return _check_google_adk_key(settings)
elif backend == "openai_agents":
    return _check_openai_agents_key(settings)
elif backend in ("codex_cli", "opencode", "copilot_sdk"):
    return HealthCheckResult(status="ok", message=f"{backend} manages its own credentials", ...)
```

Backends that manage their own credentials (Codex, OpenCode, Copilot) return `ok` without checking for an API key — these tools handle authentication internally. This prevents false-positive warnings for users of those backends.

For `claude_agent_sdk`, the check also handles the `claude_sdk_provider` setting: if the provider is a non-Anthropic router (e.g., LiteLLM, Bedrock), no Anthropic key is required. The check message explicitly notes that OAuth tokens from Anthropic's Free/Pro/Max consumer plans are not permitted for third-party use — this prevents users from copy-pasting their personal Claude token.

Legacy backend names are detected via `_LEGACY_BACKENDS` and return a `warning` with migration instructions rather than a hard failure, giving users time to update their configuration.

## check_api_key_format

```python
for field_name, validator in _API_KEY_PATTERNS.items():
    value = getattr(settings, field_name, None)
    pattern = validator["pattern"]
    if value and isinstance(value, str) and not pattern.match(value):
        warnings.append(f"{field_name} doesn't match expected format ({pattern.pattern})")
```

This check catches common mistakes like truncated keys (copied from a browser that cut the tail) or keys pasted into the wrong field (e.g., an OpenAI key in the Anthropic field). The check uses regex prefix patterns rather than full validation — it can't verify a key is valid without making an API call.

## check_backend_deps

Checks that the Python package required by the selected backend is importable:

```python
_BACKEND_DEPS = {
    "claude_agent_sdk": ("claude_agent_sdk", "claude-agent-sdk"),
    "google_adk": ("google.adk", "pocketpaw[google-adk]"),
    "openai_agents": ("agents", "pocketpaw[openai-agents]"),
}
```

A missing backend package returns `critical` — the agent loop cannot function without it. The `fix_hint` includes the exact `pip install` command, making the error actionable without documentation lookup.

## check_secrets_encrypted

This check validates that `~/.pocketpaw/secrets.enc` contains a valid Fernet-encrypted token:

```python
if text.startswith("gAAAA"):
    return HealthCheckResult(status="ok", ...)
try:
    json.loads(text)
    return HealthCheckResult(status="warning", message="Secrets file contains plaintext JSON", ...)
except (json.JSONDecodeError, ValueError):
    pass
```

Fernet tokens always start with version byte `0x80`, which base64url-encodes to `gAAAA`. Checking this prefix is faster and more reliable than attempting decryption. The fallback `json.loads` detects the case where an old version of PocketPaw wrote plaintext JSON secrets — a security regression that gets surfaced as a warning rather than silently accepted.

## Known Gaps

- `check_api_key_format` warns but does not block. A malformatted key will pass through to the agent loop, which will fail on the first API call. Consider a `critical` status for obviously wrong formats.
- The secrets check detects plaintext JSON but has no automatic migration path — users must re-save in the dashboard manually.