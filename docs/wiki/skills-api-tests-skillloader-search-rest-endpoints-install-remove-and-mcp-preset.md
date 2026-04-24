---
{
  "title": "Skills API Tests — SkillLoader Search, REST Endpoints, Install, Remove, and MCP Presets",
  "summary": "This test file covers `SkillLoader.search()` and the full skills REST API: listing installed skills, searching the skills.sh library, installing skills via git clone, removing skills, force-reloading, and MCP preset configuration with `needs_args` metadata. Tests validate filtering, case-insensitive matching, subprocess handling, and preset response shapes.",
  "concepts": [
    "SkillLoader",
    "search",
    "REST API",
    "skills install",
    "skills remove",
    "skills reload",
    "skills.sh",
    "MCP presets",
    "needs_args",
    "user-invocable",
    "git clone",
    "subprocess mocking",
    "dashboard API"
  ],
  "categories": [
    "testing",
    "skills",
    "REST API",
    "MCP integration",
    "test"
  ],
  "source_docs": [
    "e37fda50e5265450"
  ],
  "backlinks": null,
  "word_count": 509,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_skills_api.py` (created 2026-02-12) tests the skills discovery and management layer. Beyond the loader tests in `test_skills.py`, this file covers the REST surface: `GET /api/skills`, `GET /api/skills/search`, `POST /api/skills/install`, `POST /api/skills/remove`, `POST /api/skills/reload`, and the MCP preset endpoint.

## SkillLoader.search()

`TestSkillLoaderSearch` uses a pre-loaded loader with four skills (three user-invocable, one internal):

- **Empty query** — returns all user-invocable skills (3), excluding the internal-only skill.
- **Search by name** — `"commit"` returns only the commit skill.
- **Search by description** — `"code"` matches `"code-review"` whose description contains `"code changes"`.
- **Case-insensitive** — `"COMMIT"` matches `"commit"`.
- **Partial match** — `"web"` matches `"web-design"`.
- **No match** — returns empty list.
- **Excludes non-invocable** — `"internal"` doesn't return `"internal-tool"` because `user_invocable=False` skills are excluded from search results.
- **Multiple matches** — a broad term returns all matching skills.
- **Name and description match** — a term matching both name and description doesn't duplicate results.

## REST Endpoints

`TestSkillsRESTEndpoints` calls endpoint handler functions directly (not via HTTP client), using a mocked `SkillLoader`:

- **List installed** — `GET /api/skills` returns the list of loaded skills as JSON.
- **Reload** — `POST /api/skills/reload` triggers `loader.reload()` and returns a success message.
- **Search skills library — empty query** — proxies to skills.sh; empty query is handled gracefully.
- **Search skills library** — non-empty query proxies to the skills.sh API and returns results.
- **Install — missing source** — `POST /api/skills/install` without a `source` field returns 422.
- **Install — invalid source** — non-URL source returns an error.
- **Install — success** — mocks `fake_clone` to simulate a successful git clone; asserts the loader reload is triggered after installation.
- **Install — clone failure** — `fake_clone` raises; the endpoint returns an error without crashing.
- **Remove — missing name** — 422.
- **Remove — invalid name** — returns an error (name validation runs before filesystem access).
- **Remove — success** — skill directory is removed and loader is reloaded.
- **Remove — not found** — gracefully returns not-found rather than a filesystem error.

```python
async def test_install_skill_success(mock_loader):
    # fake_clone simulates git clone; asserts loader.reload() is called after
```

## MCP Preset `needs_args` Metadata

`TestMCPPresetNeedsArgs` tests the MCP (Model Context Protocol) preset endpoint. MCP presets are pre-configured integrations; some require user-supplied arguments (like a database URL) and some don't.

- **`filesystem` needs args** — the filesystem MCP preset requires a `root_path` argument.
- **`postgres` needs args** — requires a connection string.
- **`sqlite` needs args** — requires a database file path.
- **`github` does not need args** — uses environment variables; no runtime argument needed.
- **`needs_args` in response** — the preset response JSON includes a `needs_args` boolean field so the dashboard can prompt the user appropriately.

## Known Gaps

No `TODO` or `FIXME` markers. The install endpoint uses `npx` in production but the test mocks the subprocess; actual npx behavior and failure modes (missing npm, network timeout, invalid package name) are not covered. The skills.sh proxy test uses a mock rather than a real HTTP call, so API changes in skills.sh would not be caught.
