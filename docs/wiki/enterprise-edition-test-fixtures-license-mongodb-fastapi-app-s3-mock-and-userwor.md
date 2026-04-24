---
{
  "title": "Enterprise Edition Test Fixtures: License, MongoDB, FastAPI App, S3 Mock, and User/Workspace Factories",
  "summary": "This conftest provides the full test infrastructure for PocketPaw's enterprise edition integration tests: HMAC-signed license key injection, isolated in-memory MongoDB via mongomock-motor, a mounted FastAPI app with mocked agent pool, an async HTTP client, callable factories for users and workspaces, and a session-scoped S3 mock via moto.",
  "concepts": [
    "license key",
    "HMAC validation",
    "mongomock-motor",
    "beanie",
    "FastAPI test app",
    "moto S3 mock",
    "user factory",
    "workspace factory",
    "session fixtures",
    "callable fixtures"
  ],
  "categories": [
    "testing",
    "enterprise edition",
    "fixtures",
    "MongoDB",
    "test"
  ],
  "source_docs": [
    "59cb528ac5ec54df"
  ],
  "backlinks": null,
  "word_count": 504,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The enterprise edition (`ee/cloud`) requires a license key, a running MongoDB, AWS credentials, and a mounted FastAPI router tree. None of these should touch real infrastructure during tests. This conftest constructs a complete hermetic environment for every `tests/ee/test_*.py` file.

## Graceful Dependency Skip Guards

```python
def _require_enterprise() -> None:
    for module in ("beanie", "motor", "mongomock_motor"):
        if importlib.util.find_spec(module) is None:
            pytest.skip(f"Shared ee fixtures require the '{module}' module...")
```

The `_require_enterprise` and `_require_moto` guards emit `pytest.skip` rather than `ImportError` when optional dependencies are missing. This means a developer running `uv sync` without `--extra enterprise` sees `SKIPPED` in their output instead of a cascade of import errors. The skip is intentional and informative — it tells the developer exactly what to install rather than crashing with a traceback.

## HMAC-Signed License Key

```python
def _make_license_key(secret: str = "test-secret") -> str:
    payload = {"org": "test-org", "plan": "enterprise", "seats": 100, "exp": ...}
    sig = hashlib.sha256(f"{secret}:{payload_str}".encode()).hexdigest()
    return base64.b64encode(f"{payload_str}.{sig}".encode()).decode()
```

The license middleware validates HMAC signatures on every request. Tests need a license key that passes validation without calling a real licensing server. `_make_license_key` mints a valid key using the same algorithm the middleware expects, so license checks pass transparently.

The `license_env` fixture injects the key into `os.environ` and also clears the module-level `_cached_license` cache. Without cache invalidation, a test that runs after a license-less test inherits a cached `None` and all license checks fail even though the environment now has a valid key.

## MongoDB Isolation

```python
async def beanie_test_db():
    db_name = f"test_ee_shared_{uuid.uuid4().hex[:8]}"
    client = AsyncMongoMockClient()
    db = client[db_name]
    # Shim mongomock-motor's list_collection_names to drop unknown kwargs
    ...
    await init_beanie(database=db, document_models=[*ALL_DOCUMENTS, MemoryFactDoc])
    yield db
```

Each test gets a uniquely-named in-memory database (UUID suffix ensures no collision). The `list_collection_names` shim exists because Beanie >=1.26 passes `authorizedCollections` and `nameOnly` kwargs that mongomock-motor's stub rejects. Wrapping the method to drop unknown kwargs is a documented workaround that prevents the entire test suite from failing on Beanie version upgrades.

## FastAPI App Fixture

The `app` fixture mounts the real `ee/cloud` router tree onto a fresh `FastAPI` instance with the agent pool mocked out. This gives tests the real authentication, license enforcement, and business logic without spinning up a worker pool.

## Callable Factory Fixtures

`user_token_pair`, `workspace_factory`, and `seeded_channel` are fixtures that return async callables rather than pre-built objects. This pattern lets a single test create multiple independent users, workspaces, and channels with unique identities:

```python
async def test_multi_user_scenario(user_token_pair, workspace_factory):
    alice = await user_token_pair()
    bob = await user_token_pair()
    ws = await workspace_factory(alice)
```

Teardown is implicit — the per-test mongomock database is garbage-collected when `beanie_test_db` unwinds, so no explicit cleanup is needed.

## Session-Scoped S3 Mock

`mock_s3` uses moto's `mock_aws` context manager at session scope to intercept all boto3 S3 calls for the entire test session. Tests should use uniquely-named S3 buckets to avoid cross-test pollution within the session.

## Known Gaps

The `fleet_installed_org` and `drive_connected_pocket` factory fixtures mentioned in the header comment are deferred to Wave 3. Tests that need a fully-wired fleet or Drive connector must construct those stubs themselves.