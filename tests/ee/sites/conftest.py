# tests/ee/sites/conftest.py
# Created: 2026-06-18 (feat/sites-dedupe-migration, PERF-2).
# Updated 2026-07-17 (feat/sites-native-artifact-no-build): added two autouse fixtures
#   for the native-artifact read-through cache — ``_artifact_store_tmp`` points
#   PAW_SITES_ARTIFACT_DIR at a temp dir so a cache MISS never writes the real home, and
#   ``_captured_prewarms`` patches the pre-warm scheduler to capture (not run) scheduled
#   pre-warm coroutines so the suite never spawns a stray background build; a pre-warm
#   test requests it by name to assert scheduling / await the coroutine to run it.
# Updated 2026-06-24 (BC-9): added an autouse ``_recording_bus_for_sites`` fixture.
#   ``publish_pocket`` now emits ``SitePublished`` after a live publish, and
#   ``emit()`` asserts a bus is initialised; this installs a recording bus for the
#   whole sites tree (mirroring tests/cloud/conftest.py) so publish tests don't
#   trip that guard.
# Merged 2026-06-17 (fix/sites-plan-gate-asymmetry) — UNION of the PERF-2 Beanie
# settings fixture and the plan-gate default-plan fixture (see below).
#
# PERF-2's ``test_dedupe.py`` has PURE picker tests (``pick_canonical``) that build
# in-memory ``Site`` docs WITHOUT the function-scoped ``beanie_test_db`` DB fixture
# — the picker never touches the DB. But Beanie's ``Document.__init__`` resolves the
# document's collection settings, which raises ``CollectionWasNotInitialized`` until
# ``init_beanie`` has run at least once in the process. The async tests in the same
# file DO take ``beanie_test_db``, but it is function-scoped (its mongomock client
# goes out of scope on teardown), so a pure test that runs in isolation — or after
# that teardown — has no initialised settings and cannot even CONSTRUCT a ``Site``.
#
# This session-scoped autouse fixture (``_sites_beanie_settings``) runs
# ``init_beanie`` ONCE against a throwaway mongomock database so model CONSTRUCTION
# works everywhere in ``tests/ee/sites/``. It does NOT replace ``beanie_test_db``:
# the per-test fixture still re-inits against its own fresh DB for the async tests'
# DB isolation (Beanie re-init just repoints the settings at the new client). This
# fixture only guarantees the settings exist so a fixtureless pure test can build a
# doc.
#
# fix/sites-plan-gate-asymmetry — Sites is now plan-gated at the service
# chokepoint: sites.service.publish()/publish_pocket() and the create MCP handlers
# call require_sites_plan(), which reads the workspace plan via
# workspace_service.get_workspace_plan and raises Forbidden('plan.feature_denied')
# (or NotFound when the workspace is missing). The existing mechanics tests in this
# directory call publish/create with SYNTHETIC workspace ids that have no seeded
# Workspace doc, so the gate would now raise NotFound and mask what they actually
# exercise. The ``_default_sites_plan`` autouse fixture patches get_workspace_plan
# to return a plan that INCLUDES Sites ("business") by default, so the happy-path
# mechanics tests pass the gate. The dedicated gate test (test_plan_gate.py)
# overrides the plan per-test with its own patch (an inner patch of the same target
# wins while active), so the team-plan denial cases still assert correctly.

from __future__ import annotations

import base64
import re
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

pytest.importorskip("pocketpaw_ee")


@pytest.fixture(scope="session", autouse=True)
async def _sites_beanie_settings() -> Any:
    """Initialise Beanie once for the sites test module so in-memory ``Site``
    construction works in the pure (fixtureless) picker tests."""
    from beanie import init_beanie
    from mongomock_motor import AsyncMongoMockClient
    from pocketpaw_ee.cloud.memory.documents import MemoryFactDoc
    from pocketpaw_ee.cloud.models import ALL_DOCUMENTS

    client = AsyncMongoMockClient()
    db = client["test_ee_sites_settings"]

    original = db.list_collection_names

    async def _safe_list_collection_names(*_args: Any, **_kwargs: Any) -> list[str]:
        return await original()

    db.list_collection_names = _safe_list_collection_names  # type: ignore[method-assign]

    await init_beanie(database=db, document_models=[*ALL_DOCUMENTS, MemoryFactDoc])
    yield


@pytest.fixture(autouse=True)
def _artifact_store_tmp(tmp_path, monkeypatch):
    """Redirect the native-artifact cache root (PAW_SITES_ARTIFACT_DIR) to a per-test
    temp dir (feat/sites-native-artifact-no-build), mirroring how the build dir is kept
    out of the real ~/.pocketpaw. get_native_artifact's default filesystem store writes
    the rendered {body_html, css} here on a cache MISS, so without this a test that
    exercises the miss path would pollute the developer's real home."""
    monkeypatch.setenv("PAW_SITES_ARTIFACT_DIR", str(tmp_path / "site-artifacts"))
    yield


@pytest.fixture(autouse=True)
def _captured_prewarms(monkeypatch):
    """Capture (do NOT run) native-artifact pre-warm coroutines the sites service
    schedules (feat/sites-native-artifact-no-build).

    ``apply_leaf_edits`` / ``edit_svelte_component`` / a live svelte ``publish_pocket``
    fire a background pre-warm via ``_default_prewarm_scheduler``. In production that
    detaches an ``asyncio`` task; in tests we DON'T want a stray background build, so
    this patches the scheduler to append each scheduled coroutine to a list instead.
    A pre-warm test requests this fixture by name to assert one was scheduled and, if it
    wants the pre-warm to actually run + populate the store, ``await``s the captured
    coroutine itself. Un-awaited coroutines are closed on teardown so no "coroutine was
    never awaited" warning leaks into unrelated tests."""
    from pocketpaw_ee.sites import service as sites_service

    captured: list = []
    monkeypatch.setattr(
        sites_service, "_default_prewarm_scheduler", lambda coro: captured.append(coro)
    )
    yield captured
    for coro in captured:
        coro.close()


@pytest.fixture(autouse=True)
def _recording_bus_for_sites():
    """Install a RecordingBus for every sites test (BC-9).

    ``sites.service.publish_pocket`` now emits ``SitePublished`` after a live
    publish, and ``emit()`` raises ``AssertionError`` when no bus is initialised
    (a deliberate "forgot init_realtime" guard). The cloud test tree installs a
    recording bus autouse via ``tests/cloud/conftest.py``; mirror it here so the
    sites-tree publish tests don't trip that guard. A test that wants to assert on
    emitted events can request this fixture by name to read ``bus.events``."""
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod
    from pocketpaw_ee.cloud._core.realtime.events import Event

    class _RecordingBus:
        def __init__(self) -> None:
            self.events: list[Event] = []

        async def publish(self, event: Event) -> None:
            self.events.append(event)

        def subscribe(self, event_type: str, handler) -> None:  # noqa: ARG002
            return

    rec = _RecordingBus()
    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = rec  # type: ignore[attr-defined]
    yield rec
    bus_mod._bus = prev  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _default_sites_plan(request: pytest.FixtureRequest):
    """Default the workspace plan to one that unlocks Sites ('business') for the
    sites mechanics tests, so the new service-level plan gate doesn't reject their
    synthetic workspace ids. test_plan_gate.py patches the same target per-test to
    exercise the denial paths."""
    # The gate test owns the plan itself — don't double-patch under it.
    if request.module.__name__.endswith("test_plan_gate"):
        yield
        return
    from unittest.mock import patch

    with patch(
        "pocketpaw_ee.cloud.workspace.service.get_workspace_plan",
        new=AsyncMock(return_value="go"),
    ):
        yield


# Updated 2026-07-23 (SI-FIX — wire the import rewire pipeline): a shared stub for
# the paw-sites ``import`` generator subcommand. The real generator_client.run_import
# shells out to paw-sites-gen (Bun); this deterministic stand-in lets the import +
# crawler tests exercise the backend WIRING (publish receives the REWIRED source, the
# authoritative report persists) without Bun on PATH. Real rewire fidelity is proven
# by paw-sites' own tests + the cross-repo integration check.
def stub_import_result(
    files: dict, *, site_id: str, capture_api_base: str, capture_signed_key: str
) -> dict:
    source: dict[str, str] = {}
    assets: dict[str, str] = {}
    pages: list[dict[str, str]] = []
    forms: list[dict[str, Any]] = []
    scripts: list[str] = []
    asset_bytes = 0
    for path in sorted(files):
        raw = base64.b64decode(files[path])
        if path.endswith((".html", ".htm")):
            text = raw.decode("utf-8")
            m = re.search(r"action=['\"]([^'\"]+)['\"]", text)
            if m:
                text = re.sub(
                    r"action=['\"][^'\"]+['\"]",
                    f"action='{capture_api_base}/capture/form'",
                    text,
                    count=1,
                )
                text = text.replace("<form ", f"<form data-paw-original-action='{m.group(1)}' ", 1)
                forms.append({"page": path, "original_action": m.group(1), "rewired": True})
            source[path] = text
            tm = re.search(r"<title>([^<]*)</title>", text)
            pages.append({"path": path, "title": (tm.group(1) if tm else "").strip()})
        elif path.endswith((".css", ".js", ".txt", ".json", ".xml", ".svg")):
            source[path] = raw.decode("utf-8")
            if path.endswith(".js"):
                scripts.append(path)
        else:
            assets[path] = files[path]
            asset_bytes += len(raw)
    return {
        "source": source,
        "assets": assets,
        "report": {
            "pages": pages,
            "asset_count": len(assets),
            "asset_bytes": asset_bytes,
            "forms": forms,
            "scripts": scripts,
            "warnings": [],
        },
    }


@pytest_asyncio.fixture
async def _fake_run_import(monkeypatch):
    """Stub generator_client.run_import with the deterministic rewire above."""
    from pocketpaw_ee.sites import generator_client

    async def _run(files, **kw):
        return stub_import_result(files, **kw)

    monkeypatch.setattr(generator_client, "run_import", _run)
