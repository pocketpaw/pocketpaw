---
{
  "title": "HTTP Utilities — Secure Request Detection via Forwarded Headers",
  "summary": "`http_utils.py` provides `is_request_secure()`, a FastAPI helper that determines whether an incoming request arrived over HTTPS by inspecting both the direct URL scheme and reverse proxy forwarding headers (`X-Forwarded-Proto` and RFC 7239 `Forwarded`). This is essential for correctly setting secure cookies and enforcing HTTPS-only features when PocketPaw runs behind a tunnel or proxy.",
  "concepts": [
    "HTTPS detection",
    "X-Forwarded-Proto",
    "RFC 7239 Forwarded header",
    "reverse proxy",
    "secure cookies",
    "FastAPI",
    "request security",
    "tunnel",
    "TLS termination",
    "proxy trust"
  ],
  "categories": [
    "HTTP utilities",
    "security"
  ],
  "source_docs": [
    "03f1b96566a6a2c3"
  ],
  "backlinks": null,
  "word_count": 446,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

When PocketPaw is deployed behind a reverse proxy or tunnel (e.g., Cloudflare, nginx, ngrok), FastAPI's `request.url.scheme` will report `http` even though the end-user connected over HTTPS. The proxy terminates TLS and forwards the request internally over plain HTTP. Without compensating for this, any HTTPS-dependent feature — secure cookie flags, CSP headers, OAuth redirect URI validation — would incorrectly behave as if the connection were insecure.

`is_request_secure()` resolves this by checking three sources in priority order.

## Check Order

### 1. Direct URL Scheme

```python
if request.url.scheme == "https":
    return True
```

The fast path: if FastAPI itself sees `https://`, the connection is directly TLS-terminated at the application. No proxy involved.

### 2. X-Forwarded-Proto Header

```python
raw_forwarded_proto = request.headers.get("x-forwarded-proto")
if raw_forwarded_proto:
    first_hop_proto = raw_forwarded_proto.split(",", maxsplit=1)[0].strip().lower()
    if first_hop_proto == "https":
        return True
```

`X-Forwarded-Proto` is the de facto standard header set by nginx, Cloudflare, AWS ALB, and most other proxies. It can be comma-separated if multiple proxies are in the chain (e.g., `"https, http"`). Splitting on the first comma and taking the leftmost value corresponds to the outermost (client-facing) proxy's protocol — the one that matters for security decisions.

Lowercasing before comparison handles edge cases where a proxy sends `HTTPS` or `Https`.

### 3. RFC 7239 Forwarded Header

```python
raw_forwarded = request.headers.get("forwarded")
first_forwarded_entry = raw_forwarded.split(",", maxsplit=1)[0]
for item in first_forwarded_entry.split(";"):
    key, _, value = item.partition("=")
    if key.strip().lower() != "proto":
        continue
    proto = value.strip().strip('"').lower()
    return proto == "https"
```

RFC 7239 standardizes the `Forwarded` header with structured key-value pairs: `Forwarded: for=192.0.2.60;proto=https;by=203.0.113.43`. This check parses the first entry (outermost proxy) and extracts the `proto` field. Quoted values (e.g., `proto="https"`) are handled by stripping surrounding double quotes.

## Security Trust Note

The code includes an explicit trust annotation in the docstring:

> Trust note: forwarded headers are only reliable when PocketPaw is deployed behind a trusted reverse proxy/tunnel that overwrites these headers.

This is a known attack surface: a malicious client could send `X-Forwarded-Proto: https` directly to trick the application into thinking the request is secure when it is not. The function is correct only when the proxy is configured to overwrite (not append to) these headers before forwarding. PocketPaw's deployment documentation should reflect this requirement.

## Usage

`is_request_secure()` is consumed by session cookie logic and OAuth redirect URI construction, where the `secure=True` flag on cookies must match the actual connection security to function correctly in browsers.

## Known Gaps

- There is no configuration option to force-trust or force-distrust forwarded headers. Sites with specific proxy configurations (e.g., internal proxies that do not set `X-Forwarded-Proto`) may need to override this logic.
- The function does not check `X-Forwarded-Ssl: on`, another non-standard but common header used by some older proxies.
