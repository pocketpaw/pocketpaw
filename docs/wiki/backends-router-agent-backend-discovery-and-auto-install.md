---
{
  "title": "Backends Router — Agent Backend Discovery and Auto-Install",
  "summary": "The backends router exposes the registry of agent backends (Claude, Codex CLI, OpenCode, etc.) to the dashboard and API clients, including real-time availability checking that verifies both Python SDK imports and CLI binary presence. An install endpoint allows auto-installing missing backend SDKs via pip without requiring manual server restarts.",
  "concepts": [
    "agent backend",
    "backend registry",
    "availability check",
    "CLI binary",
    "shutil.which",
    "pip install",
    "safe_install_error",
    "Capability flags",
    "admin scope",
    "auto-install",
    "Codex CLI",
    "OpenCode"
  ],
  "categories": [
    "API",
    "Agent Runtime",
    "Security"
  ],
  "source_docs": [
    "2fcc4aa12b87e4a0"
  ],
  "backlinks": null,
  "word_count": 369,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw supports multiple agent backends — different AI engines or CLI tools that handle the actual reasoning loop. The backends router provides a unified REST interface for discovering which backends are registered, checking their real availability, and installing missing dependencies on-demand.

## Availability Checking Beyond Import Success

A backend being registered in the registry does not mean it can actually run. `_check_available(info)` performs two independent checks:

1. **Python import check**: Attempts to import the module specified in `install_hint.verify_import`. If the import fails, the backend is unavailable. It also checks for a specific attribute (`verify_attr`) on the imported module — useful when a package installs successfully but a deprecated or renamed class is expected.

2. **CLI binary check**: Some backends (Codex CLI, OpenCode, GitHub Copilot SDK) require an external binary in addition to the Python package. `_CLI_BINARY` maps backend names to expected binary names, and `shutil.which()` verifies presence on `PATH`.

```python
binary = _CLI_BINARY.get(info.name)
if binary and not shutil.which(binary):
    return False
```

This two-layer check prevents the dashboard from showing a backend as "available" when only the Python wrapper is installed but the underlying CLI tool is absent — which would cause confusing runtime failures.

## Auto-Install via `install_backend`

The `install_backend` endpoint accepts a backend name and triggers a `pip install` subprocess asynchronously. The `safe_install_error` helper from `pocketpaw.security.redact` sanitizes any error output before returning it to the client, preventing internal path disclosure or package manager internals from leaking through error messages.

This endpoint requires the `admin` scope via `require_scope`, preventing unprivileged API keys from triggering arbitrary package installs — which could be used as a vector for supply chain attacks if left open.

## Capability Flags

Each backend exposes a set of `Capability` flags (streaming, tool use, vision, etc.) that the dashboard uses to decide which features to surface. The list endpoint includes these alongside the availability status and display name.

## Known Gaps

The install endpoint runs `pip install` directly into the server's running Python environment. There is no sandboxing or version pinning, meaning a malicious or corrupted package index entry could be installed. In the broader workspace security policy, package installs are subject to minimum release age checks, but the API itself does not enforce this.