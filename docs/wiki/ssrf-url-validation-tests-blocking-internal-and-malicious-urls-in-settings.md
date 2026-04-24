---
{
  "title": "SSRF URL Validation Tests: Blocking Internal and Malicious URLs in Settings",
  "summary": "This test file, added during security sprint cluster E (issue #703), validates the `validate_external_url` function that guards all URL-typed settings fields against Server-Side Request Forgery (SSRF) attacks. It covers rejection of cloud metadata endpoints, RFC 1918 ranges, non-HTTP schemes, and malformed inputs, while confirming a developer-friendly escape hatch for local development.",
  "concepts": [
    "SSRF",
    "validate_external_url",
    "RFC 1918",
    "EC2 metadata",
    "POCKETPAW_ALLOW_INTERNAL_URLS",
    "URL validation",
    "Settings",
    "Pydantic ValidationError",
    "scheme enforcement",
    "developer escape hatch",
    "security sprint"
  ],
  "categories": [
    "security",
    "testing",
    "configuration",
    "SSRF prevention",
    "test"
  ],
  "source_docs": [
    "3f86a078cf15666b"
  ],
  "backlinks": null,
  "word_count": 450,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw allows operators to configure external service URLs (LLM base URLs, Signal API endpoints, etc.) via environment variables. Without validation, a malicious operator or a misconfigured deployment could point these fields at internal infrastructure — for example, the EC2 instance metadata service at `169.254.169.254` — causing PocketPaw to exfiltrate cloud credentials on their behalf. The `validate_external_url` function and these tests form the SSRF defense layer.

## Why SSRF Matters Here

Setting fields like `POCKETPAW_OPENCODE_BASE_URL` or `POCKETPAW_SIGNAL_API_URL` are read at startup and used in outbound HTTP calls made by the agent runtime. If an attacker can control these values (via environment injection, a misconfigured orchestrator, or a compromised config file), they can redirect internal HTTP calls to:
- Cloud provider metadata services (`169.254.169.254`, `fd00:ec2::254`)
- Internal Kubernetes API servers (`10.x.x.x`)
- Local file system via the `file://` scheme

## Test Classes

### `TestExternalUrlValidator`

Tests the low-level `validate_external_url(url)` function directly.

**Internal IP rejection** (`test_internal_url_rejected_when_not_allowed`): Asserts that the EC2 metadata IP, loopback (`127.0.0.1`), and RFC 1918 ranges (`10.x`, `192.168.x`) all raise `ValueError` when `POCKETPAW_ALLOW_INTERNAL_URLS=false`. This is the production default.

**Public URL acceptance** (`test_public_url_accepted`): Confirms `api.openai.com` and `example.com` pass. The function returns the URL unchanged on success, allowing Pydantic validators to use it as a transformer.

**Scheme enforcement** (`test_non_http_scheme_always_rejected`): `file://`, `ftp://`, and `gopher://` are always rejected regardless of the `POCKETPAW_ALLOW_INTERNAL_URLS` flag. The error message must include the word `"scheme"` so operators can diagnose config mistakes quickly.

**Developer escape hatch** (`test_internal_url_accepted_when_flag_set`): Setting `POCKETPAW_ALLOW_INTERNAL_URLS=true` allows localhost and RFC 1918 IPs. This is necessary for self-hosted Ollama (`127.0.0.1:11434`) and local development setups. The flag is opt-in, never the default.

**Empty string passthrough** (`test_empty_string_accepted`): An empty string means "not configured" in PocketPaw's Settings convention. Rejecting it would make all optional URL fields mandatory, which would break fresh installs.

**Malformed URL rejection** (`test_malformed_url_rejected`): Strings without a scheme or with an empty host fail. This prevents typos from silently resolving to unexpected destinations.

### `TestSettingsAppliesValidator`

Integration tests that verify `Settings()` itself fails fast when a malicious URL is supplied via environment variable — not just the low-level validator.

`test_opencode_base_url_rejects_metadata_service` sets `POCKETPAW_OPENCODE_BASE_URL=http://169.254.169.254/` and asserts `Settings()` raises. Pydantic wraps the underlying `ValueError` in a `ValidationError`, so the test catches `Exception` broadly.

`test_signal_api_url_rejects_file_scheme` confirms the `file:///etc/passwd` attack vector is blocked at the `Settings` construction boundary.

### Settings Singleton Reset

`_reload_settings()` clears `cfg._settings = None` before each integration test. PocketPaw caches the settings singleton after first load; without this reset, environment variable changes applied with `monkeypatch.setenv` would be invisible to subsequent `Settings()` calls in the same process.

## Known Gaps

No TODO or FIXME markers are present. IPv6 link-local addresses (`fe80::`) and the IPv6 loopback (`::1`) are not explicitly tested here, though they represent the same SSRF risk as their IPv4 equivalents.