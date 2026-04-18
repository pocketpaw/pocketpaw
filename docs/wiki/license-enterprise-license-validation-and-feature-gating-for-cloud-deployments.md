# license — Enterprise license validation and feature gating for cloud deployments

> This module provides cryptographic validation of signed license keys, caching of license state, and FastAPI dependency injection hooks to gate enterprise features. It exists to enforce licensing policies at runtime while maintaining a clean separation between licensing logic and business logic, enabling PocketPaw to support both open-source and commercial deployment models.

**Categories:** licensing & commercialization, authorization & access control, FastAPI integration, security & cryptography  
**Concepts:** LicensePayload, LicenseInfo, Ed25519 cryptography, HMAC-SHA256 fallback, FastAPI dependency injection, Depends(), HTTPException, require_license, require_feature, get_license_info  
**Words:** 1795 | **Version:** 1

---

## Purpose

The `license` module is the runtime enforcement layer for PocketPaw's enterprise licensing system. It solves two problems:

1. **Verification**: Ensure that license keys provided at deployment time are authentic (signed by the license server) and valid (not expired, issued to a legitimate org).
2. **Authorization**: Gate access to premium features at the HTTP endpoint level using FastAPI's dependency injection system, preventing unlicensed deployments from accessing enterprise functionality.

This module exists as a separate concern because licensing is orthogonal to core business logic—a user management service shouldn't need to know about license states. By centralizing this here, the system can:

- Use Ed25519 cryptography to verify license authenticity without storing the private key in the codebase
- Support multiple deployment models: self-hosted (HMAC-SHA256 fallback), cloud (Ed25519 verification), and open-source (no license required, endpoints return 403)
- Cache the license on first load to avoid repeated disk/env lookups
- Provide a single source of truth for license state across all endpoints

## Key Classes and Methods

### `LicensePayload(BaseModel)`

The data model representing the contents of a valid license key. It holds:

- **`org`** (str): Organization identifier (e.g., "acme-inc"), used for audit logging and multi-tenancy
- **`plan`** (str): License tier—"team" (default, 5 seats), "business", or "enterprise"
- **`seats`** (int): Number of concurrent users allowed (default 5)
- **`exp`** (str): Expiration date in ISO format (e.g., "2027-01-01")
- **`features`** (list[str]): Optional feature flags (e.g., ["analytics", "sso"])

**Key properties:**

- **`expired`** (property): Returns True if current UTC time > expiration date. Handles date parsing errors gracefully by returning True (fail-safe: invalid dates are treated as expired).
- **`has_feature(feature: str)`** (method): Returns True if the feature is in the features list OR the plan is "enterprise" (enterprise always unlocks all features). This implements the business rule that enterprise licenses are feature-complete.

### `_verify_signature(payload_bytes: bytes, signature_hex: str) -> bool`

Cryptographic validation function with a fallback chain:

1. **Primary (Ed25519)**: If `POCKETPAW_LICENSE_PUBLIC_KEY` is set, verify the signature using the public key embedded in the code. This is the secure path for cloud deployments.
2. **Fallback (HMAC-SHA256)**: If no public key is configured, compute `SHA256("<secret>:<payload>")` and compare. This allows self-hosted deployments to use a simpler symmetric key model without managing keypairs.
3. **Reject**: If neither key is available, return False (fail-safe).

The function catches all exceptions (malformed hex, cryptography library errors) and returns False, preventing crashes from bad input.

### `validate_license_key(key: str) -> LicensePayload`

The main parsing and validation entry point. It:

1. Base64-decodes the license key string
2. Splits on the last "." to separate payload from signature
3. Verifies the signature cryptographically
4. JSON-deserializes the payload into a `LicensePayload` object
5. Checks expiration
6. Raises `ValueError` with a specific message if any step fails

This is the only function that parses untrusted input, so all validation is concentrated here.

### `load_license() -> LicensePayload | None`

Startup-time license loader:

1. Returns cached license if already loaded (prevents re-parsing)
2. Attempts to load `.env` file (via `dotenv`) if available
3. Reads `POCKETPAW_LICENSE_KEY` from environment
4. Calls `validate_license_key()` and caches the result
5. Returns None if key is missing or invalid, storing the error reason in `_license_error` for later reporting
6. Logs success/failure at WARNING level so operators see licensing status in startup output

This is called during app initialization (via FastAPI startup hooks or explicit imports).

### `get_license() -> LicensePayload | None`

Lazy loader and cache getter. Returns the cached license if available; otherwise calls `load_license()`. This is safe to call on every request because the cache prevents repeated parsing.

### `async require_license() -> LicensePayload`

A FastAPI dependency that gates endpoints behind a valid license:

```python
@app.get("/api/enterprise/thing")
async def get_thing(license: LicensePayload = Depends(require_license)):
    # Only reachable if license is valid and not expired
    ...
```

Raises `HTTPException(403)` with a descriptive error message if:
- License is None (not configured)
- License is expired

The error message includes the stored license error (e.g., "Invalid signature") so operators can debug configuration issues.

### `require_feature(feature: str)`

A dependency factory that returns a specialized dependency for per-feature gating:

```python
@app.get("/api/sso/config")
async def get_sso_config(license: LicensePayload = Depends(require_feature("sso"))):
    # Only reachable if license exists, isn't expired, AND includes "sso" feature
    ...
```

Composed as: calls `require_license()` (ensures a valid license exists), then checks `license.has_feature(feature)`. Raises `HTTPException(403)` with the plan name if the feature is not included.

### `LicenseInfo(BaseModel)` & `get_license_info() -> LicenseInfo`

A read-only view of license state for the settings/admin UI:

- **`valid`** (bool): True if license exists and is not expired
- **`org`, `plan`, `seats`, `exp`** (optional): Populated from the license payload
- **`error`** (optional): Human-readable error message (e.g., "License expired", "Invalid signature")

`get_license_info()` always returns a `LicenseInfo` object (never raises), making it safe to expose via a public endpoint for UI rendering.

## How It Works

### Initialization Flow

1. **App startup**: The FastAPI app imports this module (or explicitly calls `load_license()`)
2. `load_license()` reads the environment variable and validates the key
3. The `LicensePayload` is cached in `_cached_license` and the app continues normally
4. If validation fails, `_license_error` is set and subsequent license checks return None

### Request-Time License Check

1. A client hits an endpoint decorated with `@Depends(require_license)` or `@Depends(require_feature(...))`
2. FastAPI calls the dependency function
3. The dependency calls `get_license()`, which returns the cached `LicensePayload` (fast path) or None
4. If None, an HTTPException(403) is raised; FastAPI returns a 403 response to the client
5. If valid, the endpoint handler receives the license as an argument and proceeds

### Key Data Flow

```
POCKETPAW_LICENSE_KEY (env var)
  ↓
validate_license_key()
  ├─ base64 decode
  ├─ split on "."
  ├─ _verify_signature() → cryptographic check
  └─ JSON deserialize → LicensePayload
  ↓
_cached_license
  ↓
get_license() → (used by endpoints)
  ↓
require_license() [FastAPI dependency]
  ↓
HTTPException(403) or endpoint handler
```

### Edge Cases

1. **Missing public key**: If `POCKETPAW_LICENSE_PUBLIC_KEY` is not set, the system falls back to HMAC-SHA256. This allows self-hosted installations to validate licenses without managing asymmetric keys.
2. **Unparseable dates**: If the `exp` field cannot be parsed as an ISO date, `expired` returns True (fail-safe: invalid licenses are treated as expired).
3. **Missing .env file**: The code attempts to load `.env` via `python-dotenv`, but ignores ImportError if the library isn't installed. This allows the module to work in environments where `.env` files aren't used.
4. **Expired enterprise key with no public key**: If the key format is invalid but `POCKETPAW_LICENSE_SECRET` is set, the signature check may pass, but the expiration check still fails.
5. **Concurrent requests**: The cache is not thread-locked, but loading the license twice is idempotent and safe (parsing the same environment variable twice yields the same result).

## Authorization and Security

### Cryptographic Security

- **Production (cloud)**: License keys are signed with Ed25519 (NIST-recommended, post-quantum resistant). The public key is embedded in this file; the private key exists only on the license server. An attacker cannot forge a license without the private key.
- **Self-hosted fallback**: Uses HMAC-SHA256 with a shared secret (`POCKETPAW_LICENSE_SECRET`). The secret must be provisioned out-of-band and kept confidential. HMAC is vulnerable to brute-force but acceptable for internal deployments.
- **No license**: If neither key is configured, all signature checks fail. Deployments without licensing can run open-source features but cannot access enterprise endpoints.

### Access Control

Two layers of gating:

1. **`require_license()`**: Requires a valid, non-expired license. Permits any plan (team, business, enterprise).
2. **`require_feature(feature_name)`**: Requires a valid license that explicitly includes the feature, or is on the "enterprise" plan. Per-feature access control allows granular commercialization.

### No User-Level Licensing

This module does not implement per-seat or per-user licensing (seat counting is not performed). The `seats` field in the payload is informational; it's the operator's responsibility to enforce user limits at the organization or reverse-proxy level.

## Dependencies and Integration

### Internal Dependencies

- **`fastapi`**: Used for `Depends`, `HTTPException`, and the `Request` type hint
- **`pydantic`**: Used for `BaseModel` to define `LicensePayload` and `LicenseInfo`
- **`cryptography` (conditional)**: Only imported if Ed25519 verification is attempted; if unavailable or key is invalid, falls back to HMAC
- **`python-dotenv` (optional)**: Attempts to load `.env` files; gracefully skipped if not installed
- **`datetime`**: For expiration date parsing and comparison

### What Imports This Module

Based on the import graph:

- **`__init__` (package init)**: Re-exports key functions and classes (`require_license`, `require_feature`, `get_license_info`) so they're available as `from pocketpaw.ee.cloud import require_license`
- **`router`**: A FastAPI router module that uses `require_license()` and `require_feature()` to protect enterprise endpoints

### How It Integrates

```python
# In router.py (example usage)
from fastapi import APIRouter
from .license import require_license, require_feature

router = APIRouter(prefix="/api/enterprise")

@router.get("/analytics", dependencies=[Depends(require_license)])
async def get_analytics():
    return {...}

@router.post("/sso/config", dependencies=[Depends(require_feature("sso"))])
async def set_sso_config(config: SSOConfig):
    return {...}
```

The `router` imports from `license` to decorate endpoints, ensuring that only licensed deployments can call them.

## Design Decisions

### 1. **Dual-Key Strategy (Ed25519 + HMAC)**

Rather than requiring all deployments to manage a public key, the code supports two modes:
- Cloud/SaaS: Customers get a signed license key; the public key is embedded
- Self-hosted: Customers get a secret; they compute an HMAC to verify

This lowers friction for self-hosted deployments while maintaining strong cryptographic guarantees for cloud.

### 2. **Caching the License**

The license is loaded once and cached. This avoids repeated environment variable reads and JSON parsing on every request. The cache is never invalidated (licenses are static at runtime), and there's no background refresh logic, which keeps the code simple but requires a restart to pick up license changes.

### 3. **Fail-Safe Defaults**

- Invalid dates → expired
- Missing public key + missing secret → all signatures fail
- Parsing errors → logged and cached as None

These prevent accidental security leaks if configuration is partial.

### 4. **Separation of Validation and Authorization**

- `validate_license_key()` is pure: it parses and validates structure/signature
- `require_license()` is async and raises HTTP exceptions: it enforces policy

This separation allows unit testing of validation logic independently of FastAPI's request context.

### 5. **Per-Feature Gating via Dependency Factory**

`require_feature(feature)` returns a closure-based dependency. This allows:

```python
@app.get("/sso", dependencies=[Depends(require_feature("sso"))])
@app.get("/analytics", dependencies=[Depends(require_feature("analytics"))])
```

Without the factory pattern, you'd need to hardcode the feature name inside each endpoint. The factory decouples feature names from endpoint definitions.

### 6. **License Info Endpoint (Non-Throwing)**

`get_license_info()` is designed to be called from public, unauthenticated endpoints (like a health check or settings page). It never raises, always returns a `LicenseInfo` object, and includes error messages for debugging. This lets operators diagnose licensing issues via a simple GET request.

### 7. **Global State (Cached License)**

The module uses module-level variables `_cached_license` and `_license_error`. This is stateful but acceptable because:
- Licenses don't change at runtime (no race conditions)
- All threads/workers share the same environment variable
- The cache is read-heavy (every request) and write-once (startup), favoring simplicity over locking

In a future refactor, this could be moved to a singleton service class if the app grows more complex state management.

---

## Related

- [eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints](eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints.md)
- [untitled](untitled.md)
