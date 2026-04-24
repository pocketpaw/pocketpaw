---
{
  "title": "Files Router Bootstrap Integration Test",
  "summary": "This module contains a single integration test that verifies the files router can be mounted into a FastAPI application via build_files_router and that the /files/tree endpoint responds correctly with a 200 status and a JSON body containing the expected tree structure.",
  "concepts": [
    "build_files_router",
    "FastAPI bootstrap",
    "ASGITransport",
    "httpx",
    "integration test",
    "files tree",
    "RequestContext",
    "ctx_factory",
    "router mounting",
    "smoke test",
    "async test"
  ],
  "categories": [
    "testing",
    "integration",
    "FastAPI",
    "files",
    "test"
  ],
  "source_docs": [
    "tests/cloud/files/test_bootstrap.py"
  ],
  "backlinks": null,
  "word_count": 410,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_bootstrap.py` is the top-level integration smoke test for the files subsystem router. While the provider-specific tests cover individual storage backends in isolation, this test verifies that the bootstrap wiring — `build_files_router`, the `ctx_factory`, and the FastAPI router mounting — all work together end-to-end.

## The Bootstrap Factory

`build_files_router` is the public factory function that constructs the files router with injected dependencies:

```python
app.include_router(
    build_files_router(
        uploads_store=_Store(),
        kb_service=_Kb(),
        ctx_factory=lambda req: RequestContext(
            user_id="u", workspace_id="ws_1", attributes={"role": "member"}
        ),
    ),
    prefix="/api/v1",
)
```

The test injects stub implementations of the two storage dependencies:
- `_Store` — an async generator that yields nothing (empty uploads)
- `_Kb` — a service that returns an empty document list and raises `KeyError` on `get_document`

The `ctx_factory` is a lambda that ignores the real HTTP request and returns a fixed `RequestContext`. This bypasses authentication, which is correct for a bootstrap smoke test — authentication middleware is tested elsewhere.

## The Smoke Test

```python
async def test_bootstrap_tree_endpoint_works():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/v1/files/tree")
    assert r.status_code == 200
    assert "children" in r.json()
```

`ASGITransport` (from `httpx`) drives the FastAPI app in-process without a real HTTP server. The test asserts:
1. The endpoint returns HTTP 200 — the router mounted correctly, the ctx_factory ran, and the response serialized without errors
2. The response JSON contains a `"children"` key — the tree structure is present, even if the list is empty

This test would fail if:
- `build_files_router` raises during construction (e.g., missing required arguments)
- The `/files/tree` endpoint path is wrong or the router prefix is misapplied
- The response serializer fails (e.g., a non-serializable field in the tree response)
- The `RequestContext` or provider protocol has a breaking interface change

## Why a Dedicated Bootstrap Test

The provider tests (kb, uploads) verify that individual providers produce the right data. This test verifies that the providers are correctly wired into the router and that the router produces the right HTTP response shape. It catches integration bugs that only appear when all the layers are connected.

Using `ASGITransport` rather than `TestClient` (synchronous) is consistent with the project's async-first testing approach and allows the test to be `@pytest.mark.asyncio` like all other async tests in the suite.

## Known Gaps

No TODOs or FIXMEs are present. The test does not verify ABAC filtering behavior at the HTTP layer — a future test could inject an ABAC config and assert that tagged entries are filtered from the tree for non-authorized users.
