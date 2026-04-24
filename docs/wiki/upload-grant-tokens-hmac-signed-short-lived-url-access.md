---
{
  "title": "Upload Grant Tokens: HMAC-Signed Short-Lived URL Access",
  "summary": "The signing module issues and verifies HMAC-SHA256 grant tokens that allow unauthenticated HTTP access to a specific upload for a limited time window. Grants are designed for use in raw `\u003cimg src\u003e` and `\u003ca href download\u003e` attributes where an HTTP `Authorization` header cannot be sent.",
  "concepts": [
    "grant token",
    "HMAC-SHA256",
    "verify_grant",
    "DEFAULT_TTL_SECONDS",
    "timing attack",
    "hmac.compare_digest",
    "signed URL",
    "short-lived token",
    "upload access",
    "unauthenticated access"
  ],
  "categories": [
    "uploads",
    "security",
    "authentication"
  ],
  "source_docs": [
    "152be12f91054fd1"
  ],
  "backlinks": null,
  "word_count": 492,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Standard API endpoints protect uploads behind Bearer token authentication. Browser-native elements like `<img>`, `<video>`, and `<a download>` cannot attach an `Authorization` header—they perform a plain GET. Without a signed-URL mechanism, the frontend must either proxy every media byte through an authenticated server (bandwidth and latency cost) or expose uploads without any access control. Grant tokens solve this by encoding the access permission in the URL itself.

## Token Format

Tokens on the wire take the form `{expires_unix}.{hex_hmac}`. The signed message is `{file_id}:{expires_unix}`, combined with a caller-supplied secret using HMAC-SHA256. This binds three constraints into one compact string:

- **Identity**: only files whose `file_id` is in the signed message are accessible.
- **Time**: the `expires_unix` timestamp is inside the signature, so extending or backdating the expiry without the secret is impossible.
- **Authenticity**: the HMAC ensures the token was issued by a holder of the secret.

## Verification Logic

`verify_grant` performs three checks in order:

1. Parses `expires_unix` from the token and rejects tokens past their expiry using `time.time()`.
2. Recomputes the expected HMAC for `{file_id}:{expires_unix}` and compares against the token's signature using `hmac.compare_digest`.
3. Returns `False` for any malformed token (wrong number of segments, non-integer timestamp, etc.).

Using `hmac.compare_digest` instead of `==` is critical: it runs in constant time, preventing timing side-channel attacks where an attacker probes the secret by measuring how long the comparison takes against partially-correct signatures.

## Caller-Supplied Secret

The signing secret is passed in at call time, not read from a global. This makes the module usable across both OSS (which might use the master API token as the secret) and EE (which uses its JWT signing secret) without coupling the module to either configuration model. It also makes the module trivially testable with a fixed test secret.

## `DEFAULT_TTL_SECONDS`

The module exports a `DEFAULT_TTL_SECONDS` constant (typically 300 seconds / 5 minutes) that callers use when minting grants. A short TTL limits the exposure window if a token leaks—for example in browser history, server logs, or a shared screenshot. Five minutes is long enough for a browser to load a page and render all its media, but short enough that a captured URL is useless hours later.

```python
# Minting a grant (at the route layer)
expires = int(time.time()) + DEFAULT_TTL_SECONDS
token = f"{expires}.{_sign(secret, f'{file_id}:{expires}')}"

# Verifying a grant (at the media-serving route)
if not verify_grant(file_id, token=request.query_params["t"], secret=secret):
    raise HTTPException(403)
```

## Known Gaps

- There is no token revocation mechanism. Once issued, a grant is valid until it expires naturally. If a file is deleted, the grant can still be presented—the serving route must independently confirm the file exists before streaming bytes.
- `DEFAULT_TTL_SECONDS` is a module constant, not a configuration value. Deployments that want shorter or longer windows must pass a custom expiry when minting; there is no settings-level override.
- No rate limiting on grant issuance: a client can mint unlimited grants for the same `file_id`, generating a set of simultaneously valid tokens.
