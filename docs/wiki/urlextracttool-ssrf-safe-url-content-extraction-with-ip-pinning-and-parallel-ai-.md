---
{
  "title": "UrlExtractTool: SSRF-Safe URL Content Extraction with IP Pinning and Parallel AI Fallback",
  "summary": "`UrlExtractTool` extracts clean text from URLs using Parallel AI's extract API as the primary path and a secure local HTTP fetch as fallback. The local path resolves DNS before connecting and validates the resolved IP against a public-address allowlist, preventing SSRF attacks against private infrastructure.",
  "concepts": [
    "UrlExtractTool",
    "IPPinningTransport",
    "SSRF_prevention",
    "DNS_rebinding",
    "IP_validation",
    "Parallel_AI",
    "httpx",
    "redirect_handling",
    "content_extraction",
    "TOCTOU"
  ],
  "categories": [
    "tools",
    "security",
    "web-fetching",
    "content-extraction"
  ],
  "source_docs": [
    "157d1722a16a3300"
  ],
  "backlinks": null,
  "word_count": 538,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`url_extract.py` (created 2026-02-06, Phase 1 Quick Wins) provides the `url_extract` tool for fetching and cleaning web page content. It's foundational to `ResearchTool` and any agent workflow that needs to read web pages. The implementation is notably more complex than a simple `httpx.get()` — it includes a full SSRF mitigation layer that reflects serious consideration of the threat model for an agent that can fetch arbitrary URLs.

## Dual-Path Architecture

```python
if api_key:  # Parallel AI
    return await self._extract_parallel(urls, api_key)
else:  # Local fallback
    return await self._extract_local(urls)
```

When a Parallel AI API key is configured, URL extraction is offloaded to their hosted service, which handles JavaScript rendering, bot detection, and content cleaning. The local fallback uses `httpx` directly and can only fetch static HTML — it won't work on JavaScript-rendered SPAs. This two-path design means the tool works out of the box with no configuration while offering dramatically better results with a Parallel AI key.

## SSRF Prevention: IP Validation

The local extraction path implements a full SSRF mitigation stack:

```python
async def _resolve_public_ip(hostname: str, port: int) -> str:
    raw_ip = await loop.run_in_executor(None, socket.getaddrinfo, hostname, port, ...)
    return _validate_public_ip(raw_ip)

def _validate_public_ip(raw_ip: str) -> str:
    parsed_ip = _normalize_ip_address(raw_ip)
    if not parsed_ip.is_global:
        raise ValueError("Blocked URL: resolved to non-public IP address")
    return str(parsed_ip)
```

The threat being prevented: an attacker supplies a URL like `http://metadata.internal/` or `http://169.254.169.254/latest/meta-data/` (AWS metadata service). Without IP validation, the agent would happily fetch instance credentials and return them as "page content." By resolving the hostname first and rejecting non-global IPs, the tool blocks requests to private ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), loopback (`127.0.0.0/8`), link-local (`169.254.0.0/16`), and other non-public ranges.

IPv6-mapped IPv4 addresses are normalized (`ipv4_mapped` check) to prevent the bypass where `::ffff:10.0.0.1` would pass an IPv6 check but resolve to a private IPv4 address.

## IP Pinning Transport

```python
class IPPinningTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        request.url = request.url.copy_with(host=self._pinned_ip)
        request.headers["host"] = self._host_header
        request.extensions["sni_hostname"] = self._original_host
```

After validating the IP, the transport pins the connection to that resolved IP. This closes a TOCTOU (time-of-check/time-of-use) race: without pinning, DNS could be re-queried between the validation check and the actual connection, and a DNS rebinding attack could return a different (private) IP for the second lookup. IP pinning ensures the validated IP is the one actually connected to.

## Redirect Handling

```python
_MAX_REDIRECT_HOPS = 5
```

Manual redirect following (rather than `httpx`'s built-in follow_redirects) allows IP validation at each hop. `httpx`'s automatic redirect following would re-resolve DNS without validation, creating an SSRF window. Each redirect destination is validated against the public-IP allowlist independently.

## Content Limits

```python
_MAX_CONTENT_CHARS = 50_000
```

Extracted content is capped at 50,000 characters per URL. Without this, a page that embeds megabytes of JavaScript or a very long article would exhaust LLM context. The cap is applied after content extraction and cleaning.

## Known Gaps

- The Parallel AI path has no equivalent SSRF protection — URLs are forwarded to their API without pre-validation. This is by design (they handle it) but introduces a dependency on their security posture.
- HTML parsing to extract clean text uses a simple title extractor (`_extract_title`) in the local path — full article extraction (removing nav, ads, footers) is not implemented locally.
- No robots.txt compliance check.
