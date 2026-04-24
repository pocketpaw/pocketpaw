---
{
  "title": "Cloudflare Tunnel Manager: Exposing Local PocketPaw Servers to the Public Internet",
  "summary": "The `TunnelManager` class manages a `cloudflared` subprocess to create a Cloudflare Tunnel that exposes a locally running PocketPaw server on a public HTTPS URL, enabling webhook delivery, mobile access, and external agent connectivity without firewall configuration. It supports auto-installation via Homebrew, URL extraction from stderr with a regex, graceful shutdown, and singleton lifecycle registration.",
  "concepts": [
    "TunnelManager",
    "cloudflared",
    "Cloudflare Tunnel",
    "subprocess management",
    "URL extraction",
    "regex",
    "Homebrew auto-install",
    "singleton",
    "lifecycle registration",
    "asyncio",
    "public URL"
  ],
  "categories": [
    "networking",
    "infrastructure",
    "agent runtime",
    "deployment"
  ],
  "source_docs": [
    "a37ce3941e7a884e"
  ],
  "backlinks": null,
  "word_count": 379,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's server runs locally, but many use cases require external accessibility: receiving webhooks from messaging platforms, accessing the agent from a phone, or enabling A2A agent connections from remote systems. The `tunnel.py` module solves this by wrapping the `cloudflared` CLI tool in an async process manager that handles the full lifecycle -- install, start, URL extraction, and shutdown.

## Auto-Installation

If `cloudflared` is not found on `PATH`, `TunnelManager.install()` attempts to install it via Homebrew:

```python
if shutil.which("brew") is None:
    logger.error("Homebrew not found. Cannot auto-install cloudflared.")
    return False
```

The Homebrew check prevents a confusing failure where the install subprocess itself cannot be found. Auto-install is deliberately macOS-centric; on Linux, users are expected to have `cloudflared` pre-installed.

## URL Extraction from stderr

Cloudflare Tunnel outputs its assigned URL to stderr in a multi-line box format. The `_wait_for_url()` method reads stderr line by line with a 1-second per-line timeout, applying the regex `r"https://[a-zA-Z0-9-]+\.trycloudflare\.com"` to each line. This approach is resilient to changes in the surrounding box formatting -- only the URL pattern itself matters. The overall timeout defaults to 30 seconds.

## Idempotent Start

The `start()` method guards against double-starting:

```python
if self.process:
    if self.public_url:
        return self.public_url   # Already running -- return cached URL
    await self.stop()            # Zombie process -- restart cleanly
```

This handles the case where a previous `start()` call launched the process but failed before capturing the URL, leaving a zombie subprocess.

## Graceful Shutdown

`stop()` sends `SIGTERM` first and waits up to 5 seconds before sending `SIGKILL`. `ProcessLookupError` is caught to handle the race where the process exits between the termination check and the kill. The `finally` block always clears `self.process` and `self.public_url`, ensuring `get_status()` returns `active: False` even if the kill itself fails.

## Singleton and Lifecycle Registration

`get_tunnel_manager()` returns a module-level singleton and registers it with PocketPaw's lifecycle system:

```python
register("tunnel", shutdown=_tunnel_instance.stop, reset=_reset)
```

This ensures the tunnel is properly shut down during server lifecycle events (restart, test teardown) rather than leaking background cloudflared processes.

## Known Gaps

The URL regex assumes Cloudflare's subdomain format stays stable. If Cloudflare changes the domain from `trycloudflare.com`, the regex will fail silently (URL not found -> timeout). The `asyncio.get_event_loop().time()` call inside `_wait_for_url` uses the deprecated event loop accessor; this should be `asyncio.get_running_loop().time()`. Auto-install is macOS/Homebrew only.