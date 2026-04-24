---
{
  "title": "Agents Domain FastAPI Router",
  "summary": "The agents router exposes the full HTTP API surface for managing AI agents within a workspace — CRUD, knowledge ingestion, profile picture upload, backend discovery, and scope assignment — with license gating and permission checks applied at the router and dependency level.",
  "concepts": [
    "FastAPI router",
    "license gate",
    "agent CRUD",
    "knowledge ingestion",
    "scope assignment",
    "profile picture",
    "ScopePicker",
    "require_agent_owner_or_admin",
    "backend discovery",
    "multipart upload"
  ],
  "categories": [
    "API",
    "agents",
    "enterprise"
  ],
  "source_docs": [
    "33e0ecfb418b3d9e"
  ],
  "backlinks": null,
  "word_count": 424,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## License Gate

The router is declared with a global dependency:

```python
router = APIRouter(prefix="/agents", tags=["Agents"], dependencies=[Depends(require_license)])
```

This means every endpoint under `/api/v1/agents` requires a valid enterprise license before any route handler runs. The check happens in the FastAPI dependency chain before authentication, so unlicensed calls get a clear error rather than a misleading 401.

## CRUD Endpoints

Standard create/read/update/delete routes delegate to `AgentService`. The `PATCH /{agent_id}` endpoint requires `require_agent_owner_or_admin` — only the agent's owner or a workspace admin can modify it. Delete is similarly guarded. `GET /agents` and `GET /agents/{id}` are unrestricted within an authenticated session so teammates can discover and use agents they don't own.

## Knowledge Management

Four ingestion endpoints cover the main input types:

- `POST /{agent_id}/knowledge/text` — plain text with an optional `source` label
- `POST /{agent_id}/knowledge/url` — single URL fetch-and-ingest
- `POST /{agent_id}/knowledge/urls` — batch URL ingestion (sequential, returns per-URL results)
- `POST /{agent_id}/knowledge/upload` — multipart file upload with temp-file handling
- `GET /{agent_id}/knowledge/search` — BM25 search over the agent's private scope
- `DELETE /{agent_id}/knowledge` — wipe all knowledge for an agent

File uploads are written to a temp file (using `tempfile.NamedTemporaryFile`) and always cleaned up in a `finally` block to prevent disk leaks even if ingestion raises.

## Profile Picture Upload

The avatar endpoint validates content type (JPEG, PNG, WebP only) and file size (5 MB hard cap) before writing to `~/.pocketpaw/uploads/avatars/`. The returned URL is absolute (constructed from `request.base_url`) so the frontend can render it directly without knowing the server's host. The agent's `avatar` field is updated atomically after the file is written.

## Scope Assignment Endpoints

Added in `feat/cluster-d-agent-scope-picker`:

- `GET /{agent_id}/scope` — returns the current scope list without pulling the full agent document
- `PATCH /{agent_id}/scope` — full-list replacement (not delta merge)

Both require `require_agent_owner_or_admin`. The PATCH endpoint re-validates the scope list through `ScopeAssignmentRequest` even though the frontend ScopePicker already normalises scopes. The comment is explicit: "the frontend normaliseScope helper is treated as a UX nicety, not a security boundary." Server-side validation is the authoritative gate.

## Backend Discovery

`GET /backends` introspects `pocketpaw.agents.registry` to return the list of available agent backends with their display names. This drives the backend selector in the agent creation UI.

## Known Gaps

- Batch URL ingestion (`POST /knowledge/urls`) is sequential, not concurrent. For large batches this is slow; a `asyncio.gather()` approach would be more efficient but risks rate-limiting the kb binary.
- There is no pagination on `GET /agents` — all agents in a workspace are returned in one list, which could be large for busy workspaces.