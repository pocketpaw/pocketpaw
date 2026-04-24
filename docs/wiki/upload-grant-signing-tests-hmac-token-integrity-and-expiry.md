---
{
  "title": "Upload Grant Signing Tests: HMAC Token Integrity and Expiry",
  "summary": "This small but security-critical test module verifies the `sign_grant` / `verify_grant` pair in PocketPaw's upload subsystem, ensuring that download grants are cryptographically bound to a specific file ID, expire correctly, and cannot be forged or transplanted across files.",
  "concepts": [
    "sign_grant",
    "verify_grant",
    "HMAC",
    "grant token",
    "file-scoped authorisation",
    "token expiry",
    "malformed token handling",
    "signature isolation",
    "upload signing",
    "access control"
  ],
  "categories": [
    "security",
    "file uploads",
    "testing",
    "cryptography",
    "test"
  ],
  "source_docs": [
    "4347bd383c52378b"
  ],
  "backlinks": null,
  "word_count": 481,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

When a file is uploaded in PocketPaw, the service can issue a short-lived signed grant token that authorises a specific client to download a specific file without requiring their full session credentials. `pocketpaw.uploads.signing` implements this pattern; this test file locks down its correctness properties.

## Why Signed Grants Exist

Passing a raw session token in a download URL is dangerous — URLs end up in server logs, referrer headers, and browser history. A signed grant token is scoped to a single file ID and expires after a configured TTL, limiting the blast radius if a token is accidentally exposed.

## Test Coverage

### Round-trip validity

```python
def test_sign_and_verify_roundtrip():
    token, exp = sign_grant("file-1", "secret")
    assert verify_grant("file-1", token, "secret")
    assert exp > int(time.time())
```

This is the baseline: a freshly minted token verifies successfully and its expiry timestamp is in the future. The expiry check is important — it proves the system would not issue an already-expired grant due to a clock or calculation bug.

### File-ID binding

`test_reject_wrong_file_id` mints a token for `file-1` and attempts to verify it against `file-2`. Verification must return `False`. Without this binding, a token issued for one low-sensitivity file could be reused to download a different, high-sensitivity file owned by the same user — a horizontal privilege escalation within the storage system.

### Secret binding

`test_reject_wrong_secret` mints a token with `"secret"` and verifies with a different key. This confirms the signing operation uses HMAC (or equivalent) rather than a reversible encoding scheme. An attacker who intercepts a token but does not know the signing secret cannot reuse it against a different server instance.

### Malformed token rejection

`test_reject_malformed_token` passes an arbitrary string to `verify_grant`. The function must return `False` rather than raising an exception, which means the parser handles corrupt input gracefully. This matters at the HTTP boundary where a client can send anything in a query parameter.

### Expiry

`test_reject_expired` verifies that a token whose TTL has elapsed is rejected. The exact mechanism — whether `sign_grant` accepts an explicit TTL or uses a module-level constant — is not visible in the AST, but the test confirms the expiry path is exercised.

### Cross-file signature isolation

`test_sig_not_stripped_across_file_ids` is a subtler test. It verifies that the HMAC signature component from one file ID cannot be stripped out and reattached to a token for a different file ID. This rules out a class of attacks where an adversary reconstructs a valid-looking token by combining the signature of a known-good grant with a different payload — only possible if the file ID is not included in the signed message.

## Known Gaps

No `TODO` or `FIXME` markers are present. The tests do not cover: (a) concurrent grant issuance for the same file, (b) the behaviour when the secret is an empty string, or (c) tokens with a zero or negative TTL. Clock-skew tolerance (if any) is also untested.