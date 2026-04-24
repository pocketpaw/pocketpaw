---
{
  "title": "URL Validators: SSRF Protection for Settings Fields",
  "summary": "This module provides a Pydantic validator (`validate_external_url`) that guards Settings URL fields against Server-Side Request Forgery by blocking internal, loopback, and private-range hostnames. An environment variable escape hatch allows operators to opt in to internal URLs for legitimate local deployments.",
  "concepts": [
    "SSRF protection",
    "URL validation",
    "validate_external_url",
    "internal URL blocking",
    "RFC1918",
    "Pydantic validator",
    "Settings security",
    "dotenv pre-load",
    "link-local addresses",
    "operator escape hatch"
  ],
  "categories": [
    "security",
    "configuration",
    "api"
  ],
  "source_docs": [
    "0db04305f1509649"
  ],
  "backlinks": null,
  "word_count": 472,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## The SSRF Threat in Configuration

Server-Side Request Forgery via configuration is a subtle but real attack vector: if an operator can be tricked into setting a provider URL like `http://169.254.169.254/latest/meta-data/` (AWS metadata service), PocketPaw would make requests to that internal endpoint using its IAM role — potentially leaking credentials or enabling privilege escalation.

`url_validators.py` was added as part of security cluster E (issue #703) to block this class of attack at configuration load time, before any network request is made.

## What Is Blocked

The `_host_is_internal()` function checks whether a hostname resolves to an internal address by inspecting:

- **Loopback**: `127.0.0.0/8`, `::1`
- **Link-local**: `169.254.0.0/16` (includes AWS metadata), `fe80::/10`
- **RFC1918 private ranges**: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`
- **Carrier-grade NAT**: `100.64.0.0/10`
- **Literal "localhost"** hostname

The `_ALLOWED_SCHEMES` frozenset restricts to `http` and `https` only, blocking `file://`, `ftp://`, and other schemes that could be used for local file inclusion or protocol smuggling.

## `validate_external_url`: Pydantic Integration

`validate_external_url(value)` is designed to be used as a Pydantic field validator in `Settings`:

```python
class Settings(BaseSettings):
    openai_api_base: str = validator("openai_api_base", pre=True)(validate_external_url)
```

It raises `ValueError` with a descriptive message on blocked inputs, which Pydantic surfaces as a validation error at startup — preventing the application from running with a dangerous URL configuration.

## The Dotenv Pre-Load Workaround

The module includes an unusual pattern: it calls `load_dotenv()` at import time before defining the validator. The comment explains why:

> Without this, `POCKETPAW_ALLOW_INTERNAL_URLS` set in `.env` is only read by pydantic-settings into `Settings` fields — it never reaches `os.environ`, so the validator below (which uses `os.getenv`) would miss the opt-in and block every localhost URL even when the operator set the flag.

Pydantic-settings reads `.env` into its own model fields but does not populate `os.environ`. Since the validator runs as part of Pydantic field validation (not after the model is built), it cannot access other `Settings` fields — it can only check `os.environ`. Pre-loading `.env` into the environment bridges this gap.

## `_allow_internal()`: Opt-In Escape Hatch

Operators running PocketPaw in fully local or self-hosted configurations (e.g., pointing to a local Ollama instance at `http://localhost:11434`) need to be able to use internal URLs. Setting `POCKETPAW_ALLOW_INTERNAL_URLS=true` in the environment bypasses the internal host check while still enforcing scheme validation.

## Known Gaps

- **No DNS rebinding protection**: The validator checks the URL's hostname string, not the IP it resolves to at runtime. An attacker controlling DNS could configure a public hostname that initially resolves to a public IP (passing validation) but later resolves to an internal IP when the request is made.
- **IPv6 coverage**: The blocked ranges include IPv6 loopback and link-local, but comprehensive IPv6 private range coverage (ULA `fc00::/7`) may not be complete.
- **Scheme allowlist is strict**: `http` is allowed, which means unencrypted connections to external providers pass validation. A stricter policy would require `https` for external URLs.