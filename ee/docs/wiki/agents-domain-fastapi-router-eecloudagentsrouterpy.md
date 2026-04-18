---
{
  "title": "Agents Domain FastAPI Router (ee/cloud/agents/router.py)",
  "summary": "The HTTP API layer for agent management in PocketPaw Enterprise Cloud. Provides CRUD operations, backend discovery, agent discovery with visibility rules, and a full knowledge base management API (text/URL/file ingestion, search, upload, clear).",
  "concepts": [
    "FastAPI router",
    "agent CRUD",
    "knowledge ingestion",
    "file upload",
    "license gating",
    "backend discovery",
    "batch ingestion"
  ],
  "categories": [
    "enterprise",
    "cloud",
    "API",
    "agents",
    "knowledge management"
  ],
  "source_docs": [
    "b6bf2b0946bd957c"
  ],
  "backlinks": null,
  "word_count": 487,
  "compiled_at": "2026-04-08T07:30:11Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Agents Domain FastAPI Router

## Purpose

This router defines all HTTP endpoints for the agents domain. It follows PocketPaw's domain-driven pattern: routers are thin — they parse requests, call the service layer, and return responses. Business logic lives in `service.py`.

## Route Groups

### Backend Discovery (`/agents/backends`)

Lists all registered agent backends (e.g., `claude_agent_sdk`, `openai`, custom backends). Each entry includes a display name and availability status. This feeds the frontend's backend selector dropdown. Errors during backend info retrieval are caught per-backend so one broken backend doesn't prevent listing the rest.

### CRUD (`/agents`)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/agents` | Create agent (slug uniqueness enforced in service) |
| GET | `/agents` | List agents in workspace, optional `query` filter |
| GET | `/agents/{agent_id}` | Get single agent by ID |
| GET | `/agents/uname/{slug}` | Get agent by URL-friendly slug |
| PATCH | `/agents/{agent_id}` | Update agent fields |
| DELETE | `/agents/{agent_id}` | Hard-delete agent (204 No Content) |

### Discovery (`/agents/discover`)

A POST endpoint for paginated agent browsing with visibility filtering. Supports discovering agents across visibility levels (private, workspace, public).

### Knowledge Base Management

| Method | Path | Description |
|--------|------|-------------|
| POST | `/{agent_id}/knowledge/text` | Ingest plain text |
| POST | `/{agent_id}/knowledge/url` | Ingest from URL |
| POST | `/{agent_id}/knowledge/urls` | Batch URL ingestion |
| GET | `/{agent_id}/knowledge/search` | Search with `q` parameter |
| POST | `/{agent_id}/knowledge/upload` | File upload and ingestion |
| DELETE | `/{agent_id}/knowledge` | Clear all agent knowledge |

## License Gating

The entire router is gated behind `require_license` via FastAPI's dependency injection:

```python
router = APIRouter(..., dependencies=[Depends(require_license)])
```

Every endpoint in this router requires a valid enterprise license. If the license check fails, requests are rejected before reaching any handler.

## File Upload Pattern

The `/knowledge/upload` endpoint demonstrates a secure temp-file pattern:
1. Receives the upload via FastAPI's `UploadFile`
2. Writes to a temp file with the original extension preserved (needed for format detection)
3. Passes the temp file path to `KnowledgeService.ingest_file`
4. Deletes the temp file in a `finally` block — ensures cleanup even on ingestion failure
5. Returns the original filename and size alongside the ingestion result

Supported formats: `.pdf`, `.txt`, `.md`, `.csv`, `.json`, `.docx`, `.png`, `.jpg`, `.jpeg`, `.webp`

## Known Gaps

- The `ingest_text` and `ingest_url` endpoints accept raw `dict` bodies instead of typed Pydantic schemas, losing request validation. The CRUD endpoints correctly use typed schemas.
- Batch URL ingestion (`/knowledge/urls`) processes URLs sequentially with `await` in a loop. Parallel ingestion with `asyncio.gather()` would be faster for large batches.
- No rate limiting on knowledge ingestion endpoints — a malicious or buggy client could flood the knowledge base.
- The `UploadFile` import and `upload_and_ingest` endpoint are defined after the main router section, with a bare `from fastapi import UploadFile, File as FastAPIFile` mid-file. This works but breaks the import organization convention.