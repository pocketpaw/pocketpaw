---
{
  "title": "Dashboard Starlette Compatibility Regression Test: Index Route on Starlette 1.0.0",
  "summary": "This short regression test ensures the dashboard's root route returns HTTP 200 with `text/html` content, specifically guarding against a breaking change introduced in Starlette 1.0.0 that changed the `TemplateResponse` constructor signature. The Starlette version is logged in assertion failure messages to make version-related CI failures immediately diagnosable.",
  "concepts": [
    "Starlette",
    "TemplateResponse",
    "regression test",
    "FastAPI",
    "TestClient",
    "importlib.metadata",
    "HTTP 200",
    "content-type",
    "dashboard index",
    "backward compatibility"
  ],
  "categories": [
    "testing",
    "dashboard",
    "compatibility",
    "regression",
    "test"
  ],
  "source_docs": [
    "382d72f8ae1d9439"
  ],
  "backlinks": null,
  "word_count": 391,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Starlette 1.0.0 changed the signature of `TemplateResponse` in a backward-incompatible way. Code written for Starlette 0.x that called `TemplateResponse("template.html", {"request": request})` would raise a `TypeError` on Starlette 1.0.0, which expects `TemplateResponse(request, "template.html")` or uses keyword arguments differently. This regression test exists to catch that class of breakage immediately in CI when Starlette is upgraded.

## Why a Regression Test Exists Here

Template rendering failures in FastAPI/Starlette applications often manifest as 500 Internal Server Errors rather than syntax errors at import time. Without this test, a Starlette upgrade that breaks `TemplateResponse` would pass all import-time checks and unit tests, but fail only when the dashboard is opened in a browser — which might not happen in automated CI at all.

The test is deliberately minimal: it only checks the status code and content type of `GET /`, not the HTML content itself. This makes it robust against template changes while still catching the specific failure mode (wrong status code due to a rendering exception).

## Starlette Version in Error Messages

`STARLETTE_VERSION = importlib.metadata.version("starlette")` is captured at module load time and embedded in the assertion failure message:

```python
assert response.status_code == 200, (
    f"Dashboard returned {response.status_code} "
    f"(starlette=={STARLETTE_VERSION}). "
    "Check TemplateResponse signature in dashboard.py."
)
```

This pattern makes CI failures immediately actionable: an engineer seeing a failure message like `Dashboard returned 500 (starlette==1.0.0). Check TemplateResponse signature in dashboard.py.` knows exactly where to look and why.

## Test Structure

`test_dashboard_index_returns_200` imports `pocketpaw.dashboard.app` inside the test function (not at module level) to avoid import-time errors from a broken app affecting the entire test session. A `TestClient` is created, `GET /` is called, and the status code is asserted.

`test_dashboard_index_returns_html` makes the same request and checks `"text/html" in response.headers.get("content-type", "")`. The `""` default prevents a `KeyError` if the header is absent entirely.

## Why This Pattern Is Broadly Useful

Framework version pinning and regression tests are complementary. Pinning prevents the problem but creates maintenance burden; regression tests allow upgrades while catching breakage early. This test enables confident Starlette upgrades by providing a fast, specific signal when the dashboard rendering layer breaks.

## Known Gaps

Only the root route is tested. Other dashboard routes that also use `TemplateResponse` (settings page, history view, etc.) are not covered by this regression test. A Starlette upgrade that breaks a non-root template route would not be caught here.