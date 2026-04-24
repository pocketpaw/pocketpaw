---
{
  "title": "HMAC Session Tokens: Stateless Dashboard Authentication with Instant Invalidation",
  "summary": "This module implements stateless session tokens using HMAC-SHA256 with a `{expires_unix}:{hex_hmac}` format, keyed against the master access token. Regenerating the master token instantly invalidates all outstanding sessions without requiring a server-side token store or database.",
  "concepts": [
    "HMAC session tokens",
    "stateless authentication",
    "token invalidation",
    "master access token",
    "key rotation",
    "constant-time comparison",
    "TTL",
    "timing attack prevention",
    "token format",
    "session management"
  ],
  "categories": [
    "security",
    "authentication",
    "web dashboard"
  ],
  "source_docs": [
    "f7786463406e44f3"
  ],
  "backlinks": null,
  "word_count": 513,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## The Problem: Stateless Auth Without a Database

PocketPaw's web dashboard needs session tokens — credentials that outlive a single HTTP request but expire over time. The classic approach uses a database table of active sessions, but that introduces infrastructure complexity and a central point of failure. `session_tokens.py` implements stateless sessions using a cryptographic trick instead.

## Token Format: `{expires_unix}:{hex_hmac}`

The token encodes two pieces of information:

1. **`expires_unix`**: A Unix timestamp (integer) indicating when the token expires. This is in plaintext because it needs to be readable by `verify_session_token()` without the master token — the expiry check happens before the HMAC verification to fail fast on expired tokens.

2. **`hex_hmac`**: HMAC-SHA256 of the `expires_unix` value, keyed with the master token. This ties the token's validity to the current master token.

The format is minimal by design — no JSON, no base64, no JWT claims. This reduces parsing complexity and the attack surface from malformed token deserialization.

## Instant Invalidation via Key Rotation

The master access token is used as the HMAC key. When the operator regenerates the master token (e.g., after a suspected compromise), every outstanding session token becomes invalid immediately — any HMAC verification will fail because the key changed. There is no need to iterate a sessions table and delete rows.

This is a form of implicit token versioning: all tokens are implicitly "version N" where N is the current master token value.

## `create_session_token()`

```python
def create_session_token(master_token: str, ttl_hours: int = 24, ttl_seconds: int = 0) -> str:
```

The `ttl_seconds` parameter alongside `ttl_hours` allows fine-grained TTL control for testing (e.g., `ttl_seconds=5` to create a token that expires in 5 seconds for expiry tests) without floating-point hour fractions.

## `verify_session_token()`

Verification is a two-step process:
1. Parse the token and check `expires_unix <= now` — fail fast on expiry without doing any crypto
2. Recompute `_sign(master_token, expires_unix_str)` and compare with the embedded HMAC using `hmac.compare_digest()` — constant-time comparison prevents timing attacks

The constant-time comparison is critical: a naive `==` comparison leaks information about how many bytes matched before the mismatch, allowing an attacker to brute-force the HMAC byte by byte.

## `_sign()`: Internal HMAC Helper

`_sign(key, message)` is a private function that standardizes encoding (UTF-8) and hash algorithm (SHA-256) for the HMAC operation. Centralizing this prevents subtle bugs from callers using different encodings on the key vs. the message.

## Known Gaps

- **No token revocation before expiry**: If a session token is compromised and the operator does not want to rotate the entire master token (which would log out all users), there is no mechanism to revoke just one token. The design explicitly trades per-token revocability for statelessness.
- **Expiry is in plaintext**: The `expires_unix` field is not integrity-protected independently — it is part of the HMAC input, so tampering would invalidate the signature. However, a client could inspect their own token's TTL without possessing the master secret.
- **No token binding**: Tokens are not bound to a specific IP, user agent, or device. A stolen token is fully transferable until it expires or the master token rotates.