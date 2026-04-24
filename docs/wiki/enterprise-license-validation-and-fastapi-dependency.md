---
{
  "title": "Enterprise License Validation and FastAPI Dependency",
  "summary": "A self-contained license system that validates Ed25519-signed license keys for enterprise cloud features. License state is cached process-wide and gates API endpoints via FastAPI `Depends` integration, with a fallback HMAC-SHA256 path for self-hosted deployments that cannot configure asymmetric keys.",
  "concepts": [
    "enterprise license",
    "Ed25519",
    "HMAC-SHA256",
    "LicensePayload",
    "FastAPI dependency",
    "require_license",
    "require_feature",
    "license caching",
    "license expiry",
    "feature flags",
    "POCKETPAW_LICENSE_KEY",
    "plan tiers"
  ],
  "categories": [
    "enterprise",
    "security",
    "licensing",
    "API routing"
  ],
  "source_docs": [
    "085f6f0b868e5864"
  ],
  "backlinks": null,
  "word_count": 647,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`license.py` implements the enterprise license check layer. It validates a license key on first use, caches the result, and exposes FastAPI dependency functions that gate any endpoint marked as enterprise-only. The design optimizes for the common case (valid license, stable across requests) by caching aggressively and only re-validating on startup.

## Key Format

License keys are base64-encoded strings with the form:

```
base64(payload_json + "." + signature_hex)
```

The payload is a JSON object with `org`, `plan`, `seats`, `exp`, and optional `features`. The signature is an Ed25519 signature over the raw payload bytes using a private key held only by the license server. The public key for verification is embedded in the deployment via the `POCKETPAW_LICENSE_PUBLIC_KEY` environment variable.

## Signature Verification

`_verify_signature` handles two cases:

1. **Ed25519 path** — when `POCKETPAW_LICENSE_PUBLIC_KEY` is set. Uses the `cryptography` library's `Ed25519PublicKey.verify()`. Any exception (invalid key format, bad signature, wrong bytes) returns `False` without propagating. This broad exception handling prevents an attacker from causing an unhandled exception by sending a malformed signature.

2. **HMAC-SHA256 fallback** — when the public key is absent but `POCKETPAW_LICENSE_SECRET` is set. This simpler path exists for self-hosted deployments that don't want to manage asymmetric key infrastructure. The secret must be shared out-of-band between the license issuer and the deployment.

When neither key nor secret is configured, `_verify_signature` returns `False` and no license key will validate. This fail-closed design ensures that a misconfigured deployment doesn't accidentally serve enterprise features.

## LicensePayload Model

```python
class LicensePayload(BaseModel):
    org: str
    plan: str = "team"   # team | business | enterprise
    seats: int = 5
    exp: str             # ISO date
    features: list[str] = []
```

The `expired` property catches parse exceptions and returns `True` if `exp` cannot be parsed — treating a malformed expiry as an expired license is the safer default. The `has_feature` method implements a shortcut: the `enterprise` plan implicitly grants all features, so feature flags only need to be listed for `team` and `business` plans.

## Caching Strategy

`load_license()` populates `_cached_license` on first call and returns the cached value on subsequent calls. The cache is process-wide (module-level globals), which means:

- A license change requires a process restart to take effect.
- Multiple concurrent requests that arrive before the first completes will each trigger `load_license()` — but because Python's GIL serializes dict writes, the worst case is redundant validation work, not a race condition.

The `get_settings.cache_clear()` call ensures that any `Settings.load()` calls downstream also pick up the updated backend configuration after the license is loaded.

## FastAPI Dependencies

`require_license()` is an async dependency that raises HTTP 403 with the cached error message when no valid license exists. It also re-checks expiry on every request, so a license that expires while the process is running begins rejecting requests immediately (without needing a restart).

`require_feature(feature)` wraps `require_license` and additionally checks `license.has_feature(feature)`. It returns a dependency factory (a function that returns an async function), which is the standard FastAPI pattern for parameterized dependencies.

## LicenseInfo for the Settings UI

`get_license_info()` returns a structured `LicenseInfo` model intended for display in a settings dashboard. It exposes whether the license is valid, the plan tier, seat count, and expiry, plus an error string for invalid or missing licenses.

## Known Gaps

- The public key comment in the source reads `"Replace with your actual public key"` — the default value is an empty string, meaning the Ed25519 path is always disabled unless the env var is explicitly set at deployment time.
- License revocation is not supported — a revoked key continues to work until it expires, as there is no revocation list or online validation.
- The HMAC fallback produces a weaker trust model than Ed25519 and is not suitable for production multi-tenant deployments.
- No seat count enforcement at the API layer — `seats` is stored in the payload but nothing currently checks it against the number of active users.