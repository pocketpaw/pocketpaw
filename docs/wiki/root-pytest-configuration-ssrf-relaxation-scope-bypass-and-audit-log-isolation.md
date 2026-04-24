---
{
  "title": "Root Pytest Configuration: SSRF Relaxation, Scope Bypass, and Audit Log Isolation",
  "summary": "The root `tests/conftest.py` installs three session-wide fixtures that configure PocketPaw's test environment: it relaxes SSRF URL validation so internal URLs work during tests, bypasses scope enforcement for non-security tests, and redirects the audit log to a temp file so tests never write to the developer's home directory.",
  "concepts": [
    "SSRF validation",
    "POCKETPAW_ALLOW_INTERNAL_URLS",
    "asyncio child watcher",
    "scope bypass",
    "_TESTING_FULL_ACCESS",
    "AuditLogger",
    "audit log isolation",
    "enforce_scope marker",
    "pytest fixtures",
    "autouse"
  ],
  "categories": [
    "testing",
    "security",
    "pytest configuration"
  ],
  "source_docs": [
    "1143e24d150d335c"
  ],
  "backlinks": null,
  "word_count": 514,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's root `conftest.py` is the global test bootstrap. It runs before any test in the entire suite and establishes three invariants that would otherwise require every test to handle independently. Understanding why each fixture exists requires understanding what breaks without it.

## SSRF Relaxation: `POCKETPAW_ALLOW_INTERNAL_URLS`

```python
os.environ.setdefault("POCKETPAW_ALLOW_INTERNAL_URLS", "true")
```

In production, `security.url_validators.validate_external_url` blocks loopback and RFC1918 addresses to prevent Server-Side Request Forgery attacks — an agent should never be able to point PocketPaw at `http://169.254.169.254` (AWS metadata) or `http://localhost:11434` (Ollama) on behalf of a malicious prompt. In tests, however, dozens of fixtures spin up `httpx` mock servers, Ollama stubs, and other local services on `localhost:*`. Without this environment variable, `Settings()` instantiation fails immediately on every test that constructs a config object with a local URL.

The `setdefault` (not `os.environ[...] = ...`) is intentional: tests that need strict SSRF behavior can monkeypatch `POCKETPAW_ALLOW_INTERNAL_URLS=false` themselves and the root setting doesn't override them.

## Asyncio Child Watcher: `_setup_asyncio_child_watcher`

```python
if sys.version_info < (3, 12) and hasattr(asyncio, "ThreadedChildWatcher"):
    watcher = asyncio.ThreadedChildWatcher()
    asyncio.set_child_watcher(watcher)
```

On Python 3.10 and 3.11, `asyncio` requires a child watcher to be attached to the running event loop before any subprocess can be spawned asynchronously. Tests that fork subprocesses (e.g., CLI integration tests, process-based sandboxing tests) crash with `RuntimeError: no child watcher` without this fixture. On Python 3.12+, child watchers were removed from the public API, so the guard makes the fixture a no-op on modern interpreters.

The `scope="session"` and `autouse=True` combination means this runs exactly once per test session, before the event loop is torn down.

## Scope Bypass: `_enable_test_full_access`

```python
@pytest.fixture(autouse=True)
def _enable_test_full_access(request, monkeypatch):
    if "enforce_scope" in request.keywords:
        return
    monkeypatch.setattr("pocketpaw.api.deps._TESTING_FULL_ACCESS", True)
```

PocketPaw's router dependencies check `request.state.full_access` to decide whether a caller has passed scope validation. In unit tests that mount FastAPI routers directly (without the dashboard middleware that populates `request.state`), this check always fails — every route returns `403` regardless of what the test is trying to verify.

This fixture flips a testing-bypass flag that `api.deps` reads instead of the real scope check. The `enforce_scope` marker opt-out is critical: security tests that deliberately verify the fail-closed behavior (e.g., "unauthenticated request should return 403") use `@pytest.mark.enforce_scope` to restore the real check for that test only.

## Audit Log Isolation: `_isolate_audit_log`

```python
temp_logger = AuditLogger(log_path=tmp_path / "audit.jsonl")
with (
    patch("pocketpaw.security.audit._audit_logger", temp_logger),
    patch("pocketpaw.security.audit.get_audit_logger", return_value=temp_logger),
    patch("pocketpaw.tools.registry.get_audit_logger", return_value=temp_logger),
):
    yield temp_logger
```

The `AuditLogger` singleton writes to `~/.pocketpaw/audit.jsonl` by default. Without this fixture, running the test suite would pollute the developer's persistent audit trail with thousands of fake tool invocations, making the audit log useless for post-incident review. The fixture creates a fresh `AuditLogger` pointing at `pytest`'s per-test temp directory, patches all three known references to the singleton (the module-level `_audit_logger`, `get_audit_logger`, and the tool registry's reference), and yields the temp logger so tests that need to inspect audit entries can read from it.

## Known Gaps

The `_enable_test_full_access` fixture patches a module attribute by string path, which means if `api.deps` is refactored to move `_TESTING_FULL_ACCESS` to a different module, the fixture silently stops working. There is no assertion that the patch actually took effect.