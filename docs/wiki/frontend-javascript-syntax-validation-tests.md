---
{
  "title": "Frontend JavaScript Syntax Validation Tests",
  "summary": "This module validates that all JavaScript files in PocketPaw's frontend directory have syntactically correct code by running each file through `node --check`. It also verifies structural contracts — that `app.js` defines the Alpine.js `app()` function and that `websocket.js` references `WebSocket` — catching integration breakage before it reaches users.",
  "concepts": [
    "JavaScript syntax validation",
    "node --check",
    "Alpine.js",
    "frontend/js",
    "app.js",
    "websocket.js",
    "tools.js",
    "parametrize",
    "Node.js availability",
    "dashboard frontend",
    "CI validation"
  ],
  "categories": [
    "testing",
    "frontend",
    "dashboard",
    "test"
  ],
  "source_docs": [
    "218e61ce3638d0f2"
  ],
  "backlinks": null,
  "word_count": 436,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Why JavaScript Syntax Tests Are Needed

PocketPaw's dashboard is a server-rendered HTML page that loads JavaScript files directly from the `frontend/js/` directory. Unlike a webpack or Vite project, there is no build step that would catch syntax errors at compile time. A missing bracket or an invalid `import` statement would silently break the entire dashboard UI only when a user opens their browser — potentially in production.

Running `node --check` during CI catches these errors before deployment. The test is parametrized over all JS files in the directory, so any new file added in the future is automatically included.

## Test Structure

### get_js_files()

A module-level function that uses `FRONTEND_DIR.glob("**/*.js")` to collect all JavaScript files recursively. This drives the `@pytest.mark.parametrize` decorator, so adding a new JS file automatically adds a new test case without modifying this file.

```python
FRONTEND_DIR = Path(__file__).parent.parent / "src" / "pocketpaw" / "frontend"
```

### check_node_available (autouse fixture)

All `TestJavaScriptSyntax` tests are gated on Node.js availability:

```python
@pytest.fixture(autouse=True)
def check_node_available(self):
    try:
        subprocess.run(["node", "--version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("Node.js is not available")
```

This prevents false failures on CI runners that have Python but not Node.js installed. Without this guard, the test would report a failure rather than a skip, obscuring unrelated failures.

### test_javascript_syntax

Runs `node --check <file>` for each JS file. If the return code is non-zero, the test fails with the stderr output, which includes the filename, line number, and description of the syntax error.

### File Existence Tests

Three tests assert that the canonical files exist:
- `frontend/js/app.js` — main Alpine.js application entry point
- `frontend/js/websocket.js` — WebSocket connection manager
- `frontend/js/tools.js` — tool invocation UI handlers

These existence checks catch accidental renames or deletions that would leave the HTML template with broken `<script src="...">` references.

### TestJavaScriptStructure

Two structural tests read file content and search for required identifiers:

- `test_app_defines_app_function` — `app.js` must contain `function app()` for Alpine.js's `x-data="app()"` binding to work.
- `test_websocket_defines_manager` — `websocket.js` must contain `WebSocket`, confirming the file is not accidentally empty or misnamed.

These are intentionally lightweight string checks, not AST analysis. They fail fast on catastrophic breakage (file cleared or wrong file content) without requiring a full JavaScript parser in Python.

## Known Gaps

As noted in the project instructions, `test_app_returns_object` in this file has a known pre-existing failure (`test_frontend_syntax.py::test_app_returns_object`) and is skipped in CI runs. The parametrized syntax check will fail if `get_js_files()` returns an empty list (e.g., `FRONTEND_DIR` does not exist), but that failure would be misleadingly reported as "no tests ran" rather than "directory missing". There is no test for JavaScript module exports or ES module compatibility.