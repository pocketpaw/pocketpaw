---
{
  "title": "Skills Management REST Router",
  "summary": "Provides REST endpoints for listing installed skills, searching the skills.sh marketplace, installing skills from GitHub sources, removing skills, and force-reloading the skill index. Skills are user-invocable slash commands that extend PocketPaw's capabilities without core code changes.",
  "concepts": [
    "skills management",
    "skill loader",
    "skills.sh marketplace",
    "skill installation",
    "GitHub clone",
    "slash commands",
    "SkillInstallError",
    "hot reload",
    "extensibility",
    "FastAPI router"
  ],
  "categories": [
    "api",
    "skills",
    "extensibility",
    "marketplace"
  ],
  "source_docs": [
    "5e02fe3e792f5d58"
  ],
  "backlinks": null,
  "word_count": 500,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Skills Management REST Router

The skills router is the programmatic interface to PocketPaw's extensibility layer. Skills are user-invocable slash commands — self-contained packages that can add new agent behaviors, tools, or workflows. This router handles the full skill lifecycle: discovery, installation, removal, and hot-reload.

### Listing Installed Skills

The list endpoint calls `loader.reload()` before returning results. This is not a performance oversight — it is an intentional freshness guarantee. Skills can be installed by other means (git clone, manual file copy) outside the API, and the loader caches the on-disk state. Calling reload before each list ensures the response reflects the actual filesystem state rather than a potentially stale in-memory snapshot. The response is filtered to `get_invocable()` — only skills that expose a user-facing slash command are listed, hiding internal or partial skills.

### Skills Marketplace Proxy

The `/skills/search` endpoint proxies queries to the `skills.sh` API. Rather than building a bundled skill catalog into PocketPaw, the design delegates to an external registry. This keeps the core binary small and allows the marketplace to evolve independently. The `httpx.AsyncClient` with a 10-second timeout prevents slow marketplace responses from blocking the event loop. On failure, the endpoint returns a degraded `{"skills": [], "count": 0, "error": ...}` rather than propagating the error as a 5xx. This is deliberate: a marketplace outage should not prevent users from managing their locally installed skills.

### Skill Installation

Installation is handled by `install_skill_from_source`, which clones the provided GitHub repository into the skills directory. The `SkillInstallError` exception carries a `status_code` field, allowing the installer to signal different failure modes (400 for bad source URL, 409 for already installed, 500 for git failure) through the same exception type. The router maps these directly to HTTP status codes, giving clients actionable error information.

### Removal and Reload

Removal deletes the skill's directory using `shutil.rmtree`. The force-reload endpoint triggers a full disk scan without needing to restart PocketPaw, supporting hot-reload workflows where skills are developed and tested in place.

### No Authentication on the Router

The skills router does not apply a `require_scope` dependency at the router level (unlike sessions or settings). Individual endpoints rely on the application-level authentication middleware. This is worth noting: skill installation can download and execute arbitrary code, so in a multi-user deployment the absence of explicit scope enforcement could allow any authenticated user to install skills.

### Integration Pattern

```python
# Search marketplace
GET /api/v1/skills/search?q=github

# Install from GitHub
POST /api/v1/skills/install
{"source": "https://github.com/org/my-skill"}

# Force reload after manual file changes
POST /api/v1/skills/reload
```

### Known Gaps

The remove endpoint source is truncated — the `shutil.rmtree` path shown suggests it performs no check to ensure the target directory is within the expected skills root before deleting. A path traversal guard (confirming the resolved path starts with the skills directory) should be present to prevent `name=../../important-config` style attacks. No sandbox or signature verification is applied to installed skills — any GitHub repo can be installed, giving installed skills the same filesystem and network access as the PocketPaw process.