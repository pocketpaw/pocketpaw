---
{
  "title": "UrlExtractTool: SSRF Defense, DNS Pinning, Private IP Blocking, and Multi-Provider Extraction",
  "summary": "The `UrlExtractTool` fetches and extracts text from URLs, supporting Parallel AI (cloud) and local httpx (self-hosted) providers with auto-selection. The test suite places particular emphasis on SSRF (Server-Side Request Forgery) defenses: private IP blocking, DNS TOCTOU race condition prevention via IP pinning, and IPv4-mapped IPv6 address rejection.",
  "concepts": [
    "UrlExtractTool",
    "SSRF",
    "DNS_pinning",
    "TOCTOU",
    "private_IP_blocking",
    "IPv4_mapped_IPv6",
    "redirect_protection",
    "Parallel_AI",
    "httpx",
    "html2text",
    "IP_pinning_transport"
  ],
  "categories": [
    "tool-system",
    "security",
    "testing",
    "test"
  ],
  "source_docs": [
    "75e0d5de16fe5cf6"
  ],
  "backlinks": null,
  "word_count": 517,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`UrlExtractTool` (tool name: `url_extract`) enables agents to retrieve and summarize web content. It supports three provider modes:
- **`parallel`**: Delegates to the Parallel AI extraction API (cloud, requires API key)
- **`local`**: Uses `httpx` directly with SSRF defenses (self-hosted, requires `html2text`)
- **`auto`**: Uses Parallel if an API key is configured, falls back to local

## Tool Definition

The tool's name is `"url_extract"`, trust level is `"standard"`, and it accepts a required `urls` array parameter. The schema is tested to confirm the `urls` property is typed as an array and is required.

## Parallel Provider

With `url_extract_provider="parallel"` and a configured `parallel_api_key`, the tool POSTs to the Parallel AI API and parses the `results` list. Tests cover:
- Single URL success with title and content extraction
- Multiple URLs in a single batch call
- Missing API key returns an error string
- HTTP error from the API is surfaced gracefully

## Local Provider and SSRF Defenses

The local provider is where most security complexity lives. When `html2text` is not installed, it returns a clear error rather than crashing.

### Private IP Blocking

Before fetching, the tool resolves the hostname via DNS and checks whether the resulting IP is in a private range (RFC 1918: `10.x`, `172.16-31.x`, `192.168.x`; loopback; link-local). Private IPs are blocked to prevent agents from being used to probe internal network services.

```python
async def test_local_extract_blocks_private_dns_result(...):
    # DNS resolves to 192.168.1.1 → blocked
```

### IPv4-Mapped IPv6 Blocking

An attacker can bypass `AF_INET` checks by using an IPv4-mapped IPv6 address (`::ffff:192.168.1.1`). The tool detects and blocks `AF_INET6` addresses that map to private IPv4 ranges.

### DNS TOCTOU Race Condition (Time-of-Check, Time-of-Use)

A subtle SSRF vector: DNS resolves to a public IP at check time, but a second resolution at fetch time returns a private IP (DNS rebinding attack). The tool prevents this by pinning the resolved IP at check time and passing it directly to the HTTP transport via SNI — no second DNS lookup occurs.

```python
async def test_dns_toctou_resolver_flip_is_blocked(...):
    # First DNS call → public IP, second → private IP
    # Tool should block because the pinned IP is used, not re-resolved

async def test_safe_get_pins_ip_and_avoids_second_dns_lookup():
    # Verifies exactly one DNS call is made
```

### Redirect SSRF

A redirect chain can lead from a public URL to an internal IP. The tool re-validates the IP at each redirect hop, blocking the chain when a private IP appears mid-redirect.

### IP Pinning Transport

The `test_ip_pinning_transport_rewrites_host_and_sets_sni` test verifies that the custom transport rewrites the `Host` header to the pinned IP while preserving the original hostname as the SNI for TLS — required for correct certificate validation.

## Auto Mode

`auto` mode selects `parallel` when a key is configured and `local` otherwise. Tests verify both branches.

## Edge Cases

- **Empty URLs list**: Returns immediately without making any network call.
- **Unknown provider**: Returns a clear error string identifying the unsupported provider name.

## Known Gaps

No TODOs. The DNS TOCTOU test depends on precise mock sequencing — `getaddrinfo` must be called twice with different return values, which requires careful side-effect ordering in the mock.