---
{
  "title": "Test Suite for Knowledge Ingestion: CFO Agent Creation and Document Upload Smoke Test",
  "summary": "A manual smoke test script that creates a CFO-persona agent via the REST API and ingests three fixture documents into its knowledge base. It lives in `scripts/` rather than `tests/` to keep it out of pytest collection and CI, and requires a running backend with a valid bearer token to execute.",
  "concepts": [
    "knowledge ingestion",
    "CFO agent",
    "smoke test",
    "bearer token",
    "REST API",
    "document upload",
    "httpx",
    "agent creation",
    "fixture documents",
    "cross-document reasoning",
    "manual testing"
  ],
  "categories": [
    "testing",
    "scripts",
    "knowledge",
    "integration",
    "test"
  ],
  "source_docs": [
    "ab53ecfc390eb575"
  ],
  "backlinks": null,
  "word_count": 543,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`scripts/knowledge_smoke_test.py` is an end-to-end validation of the agent creation and knowledge ingestion pipeline. It cannot be a pytest test because it requires a live server, real authentication, and fixture documents on disk — conditions that cannot be met in CI without significant infrastructure. The script is a developer tool for manually verifying that the knowledge pipeline works before shipping changes to agent creation or document ingestion.

## Why It Lives in scripts/ Not tests/

The module docstring explains this explicitly: placing it in `tests/` would cause pytest to collect it, and running it without a live server and bearer token would cause confusing failures in CI. By placing it alongside `a2a_smoke_test.py` in `scripts/`, it is visible and findable without polluting the test suite.

## Prerequisites

Three things must be in place before running:

1. **Backend running**: `uv run pocketpaw serve`
2. **Bearer token**: Pasted into the `TOKEN` constant at the top of the file
3. **Fixture documents**: `nexwrk-financials.md`, `nexwrk-product.md`, `nexwrk-team.md` in the same directory as the script

The fixture documents represent a fictional company (Nexwrk) and are designed to test that the knowledge retrieval system can answer questions spanning multiple documents with different content types (financials, product description, team roster).

## What It Tests

### Step 1: Agent Creation
POSTs to `/api/v1/agents` with a CFO persona, slug `cfo`, and a detailed system prompt. This validates that the agent creation endpoint accepts the payload shape, assigns an ID, and returns a `200` status.

### Step 2: Document Ingestion
For each of the three fixture files, the script reads the content and POSTs it to `/api/v1/agents/{agent_id}/knowledge`. This validates:
- The knowledge ingestion endpoint accepts markdown content
- Documents are associated with the correct agent
- The API handles multiple sequential uploads without error

### Step 3: Knowledge Retrieval (implicit)
After ingestion, the script sends a chat message to the CFO agent asking a question that requires cross-document reasoning. The response content is printed for manual inspection — there is no automated assertion, because evaluating LLM response quality requires human judgment.

## Bearer Token Handling

The `TOKEN` constant is left empty in source and must be filled in before running:
```python
TOKEN = ""  # Paste your bearer token here before running
```
This is intentional — hardcoding a token in source would be a security issue. The script is not intended to run in CI and does not support environment variable injection for the token.

## httpx Usage

The script uses `httpx.AsyncClient` with a 30-second timeout. The timeout is generous enough for document ingestion (which may involve chunking and embedding) but will catch hung requests. Using `async with` for the client ensures the connection pool is closed even if the script raises an exception partway through.

## Known Gaps

- The `TOKEN` must be manually pasted before every run; there is no `--token` CLI argument or environment variable support.
- There are no automated assertions on the chat response — a completely wrong answer would not fail the script.
- The fixture documents (`nexwrk-*.md`) must be manually placed in the scripts directory; they are not checked into the repository.
- The script does not clean up the CFO agent after running, which can cause duplicate agents in the development database across multiple runs.