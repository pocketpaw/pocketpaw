---
{
  "title": "Enterprise License Validation System",
  "summary": "Cryptographic license validation for PocketPaw Enterprise features. Supports Ed25519 signatures for production and HMAC-SHA256 for self-hosted deployments. Provides FastAPI dependencies for gating endpoints behind valid licenses and specific feature flags.",
  "concepts": [
    "license validation",
    "Ed25519",
    "HMAC-SHA256",
    "LicensePayload",
    "require_license",
    "require_feature",
    "FastAPI dependency",
    "feature flags",
    "enterprise gating",
    "cryptographic verification"
  ],
  "categories": [
    "licensing",
    "security",
    "enterprise",
    "authentication"
  ],
  "source_docs": [
    "f4538a40ba9933a4"
  ],
  "backlinks": null,
  "word_count": 556,
  "compiled_at": "2026-04-08T07:26:37Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Enterprise License Validation System

`cloud/license.py`

## Purpose

This module is the gatekeeper for all enterprise/cloud features. Every licensed endpoint depends on `require_license()`, which validates that a signed license key is present, not expired, and optionally has specific feature flags. The design supports two verification modes: Ed25519 (production) and HMAC-SHA256 (self-hosted).

## License Key Format

```
base64(payload_json + "." + signature_hex)
```

The payload is a JSON object:
```json
{"org": "acme-inc", "plan": "team", "seats": 10, "exp": "2027-01-01"}
```

The signature covers the payload JSON string. Using `rsplit(".", 1)` ensures dots within the JSON payload don't break parsing.

## Signature Verification

### Ed25519 (Production)

When `POCKETPAW_LICENSE_PUBLIC_KEY` env var is set, the module uses the `cryptography` library's Ed25519 verification. The public key is embedded via environment variable; the private key exists only on the license server. This is asymmetric — the application can verify but cannot forge licenses.

### HMAC-SHA256 (Self-hosted Fallback)

When no public key is configured, falls back to HMAC-SHA256 using `POCKETPAW_LICENSE_SECRET`. This is simpler to set up for self-hosted deployments but requires the shared secret on the application server, making it less secure.

If neither key is configured, verification fails — no silent bypass.

## LicensePayload Model

- `org` — Organization name
- `plan` — `"team"`, `"business"`, or `"enterprise"`
- `seats` — Licensed seat count (default 5)
- `exp` — ISO date expiration
- `features` — Optional feature flag list
- `expired` property — Compares current UTC time against `exp`
- `has_feature(feature)` — Returns `True` if the feature is in the list OR the plan is `"enterprise"` (enterprise gets everything)

## Caching

The module caches the validated license in `_cached_license` at module level. `load_license()` is called once; subsequent calls to `get_license()` return the cached result. This avoids re-parsing and re-verifying the key on every request.

The `_license_error` variable stores the last validation error message, which is returned in the 403 response and the settings UI.

## FastAPI Dependencies

### require_license()

Async dependency that returns the `LicensePayload` or raises HTTP 403. Checks both existence and expiration. Used as a router-level dependency on `_licensed` sub-routers.

### require_feature(feature)

Dependency factory that creates a feature-specific check. Uses `Depends(require_license)` internally, so it first validates the license, then checks for the specific feature flag. Enterprise plans bypass feature checks.

### get_license_info()

Returns a `LicenseInfo` object for the settings UI showing license status, plan details, and any error messages. Not a dependency — called directly by an endpoint.

## Design Decisions

- **Dual verification modes**: Ed25519 for production security, HMAC-SHA256 for simpler self-hosted setups. The fallback is explicit (no silent downgrade).
- **Module-level caching**: Avoids re-verification per request. Trade-off: a license that expires mid-runtime won't be caught until restart (but `expired` property checks on each call mitigate this).
- **Enterprise gets all features**: `has_feature` returns `True` for enterprise plans regardless of the features list, simplifying feature gating.

## Known Gaps

- `load_license()` tries to import `dotenv` and call `load_dotenv()` — this has a side effect of loading ALL env vars from `.env`, not just the license key. Could cause unexpected behavior if `.env` has conflicting values.
- Seat count is stored but never enforced — no code checks current user count against `seats`.
- No license refresh mechanism — changing the key requires a server restart.
- The HMAC fallback means a self-hosted deployment operator who knows the secret could forge licenses.
