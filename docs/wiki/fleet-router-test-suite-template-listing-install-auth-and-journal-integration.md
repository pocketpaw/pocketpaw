---
{
  "title": "Fleet Router Test Suite: Template Listing, Install Auth, and Journal Integration",
  "summary": "Covers the FastAPI fleet REST router that powers the paw-enterprise InstallFleetPanel, verifying template enumeration, end-to-end install flows, journal event emission, and a layered auth guard (401/403/200) added when the P0 auth requirement landed. Uses FastAPI dependency_overrides throughout so tests remain hermetic — no real Mongo, no real soul-protocol runtime, no writes to ~/.soul/.",
  "concepts": [
    "fleet router",
    "FastAPI TestClient",
    "dependency_overrides",
    "auth guard",
    "role-based access control",
    "journal integration",
    "FleetTemplate",
    "install report",
    "Pydantic serialisation",
    "lru_cache isolation",
    "soul_protocol",
    "ee.fleet"
  ],
  "categories": [
    "testing",
    "enterprise features",
    "fleet management",
    "authentication",
    "test"
  ],
  "source_docs": [
    "ed65da7d9b189526"
  ],
  "backlinks": null,
  "word_count": 503,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The fleet router exposes two primary endpoints: a `GET /fleet/templates` list route that returns bundled FleetTemplates in an envelope, and a `POST /fleet/install` writer that installs a named fleet and optionally emits correlated journal events. `tests/ee/test_fleet_router.py` pins every public contract those endpoints make with the paw-enterprise frontend.

## Why This File Exists

Installing a fleet is a destructive, multi-step operation: it provisions connectors, pockets, and soul factories. Before this test suite existed, regressions in the template catalogue or the install serialisation could silently break the enterprise UI. The file was first created alongside `feat/fleet-rest-router`, then extended twice — once for `feat/ee-journal-dep` to swap a brittle `_open_default_journal` monkey-patch for the production `get_journal` seam, and again for `fix/fleet-install-auth-guard` to add a role-based access control layer.

## Test Structure

### TestGetTemplates
Verifies the canonical envelope shape, asserts that at least one bundled template ("sales-fleet") is present, and checks that a deliberately malformed template is silently skipped rather than crashing the list endpoint. The skip behaviour prevents a single bad template from taking down the whole catalogue.

### TestInstallFleet
Runs the install happy path against fake soul + connector + pocket factories mounted at the `ee.fleet.router` seam. Key assertions:
- The router returns a serialised install report.
- When `journal=true` is in the request body, the install call receives a live `Journal` and the journal afterwards contains correlated events with matching `correlation_id`.
- When `journal=false` (or omitted), the installer receives `None` — the test confirms this by inspecting the mock call signature rather than checking whether the dep was resolved (the dep is always resolved; the router decides whether to forward it).
- An unknown template name produces a 404; a malformed body produces a 422.

### TestInstallFleetAuth
Added in `fix/fleet-install-auth-guard`. Uses a header-driven `current_active_user` override (`X-Test-User` + `X-Test-Workspaces`) to avoid wiring real JWT/Mongo infrastructure into route tests. Tests cover:
- No `X-Test-User` header → 401 before the installer runs.
- Valid user but not a member of the target workspace → 403.
- Member with role below `admin` → 403.
- Admin and Owner roles → 200.

### TestResponseShape
Asserts that serialising the install report via Pydantic does not raise `PydanticSerializationUnexpectedValue`. This class exists because an earlier version of the report model had an `Any`-typed field that Pydantic serialised inconsistently depending on the runtime type — a class of bug that only surfaces during serialisation, not construction.

## Fixture Design

```python
@pytest.fixture(autouse=True)
def _isolate_journal_cache():
    reset_journal_cache()
    yield
    reset_journal_cache()
```

The `lru_cache` on `get_journal` is module-global. Without the reset, a journal opened against one test's `tmp_path` would leak into the next test, masking dependency-override bugs. The autouse fixture runs before and after every test to guarantee a clean slate.

The `app` fixture uses `dependency_overrides` to point `get_journal` at a disposable SQLite file under `tmp_path`, ensuring tests never write to the real `~/.soul/` data directory.

## Known Gaps

No explicit test covers the case where the `journal.append()` call itself raises (e.g., a disk-full scenario mid-install). Error handling in that path is implicitly trusted to the `soul_protocol` library.