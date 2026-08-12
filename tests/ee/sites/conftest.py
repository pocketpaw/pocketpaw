# tests/ee/sites/conftest.py
# Created: 2026-06-18 (feat/sites-dedupe-migration, PERF-2).
# Updated 2026-08-11 (fix/tests-ignore-operator-env): added the autouse
#   ``_operator_env_cleared`` fixture. Nothing in this tree isolated the test process
#   from the developer's own environment: ``config.py`` declares ``env_file=".env"`` and
#   ``security/url_validators.py`` calls ``load_dotenv(override=False)`` at IMPORT time,
#   and dotenv searches upward from the CWD — so a gitignored ``.env`` in the checkout
#   root silently became test input. With one present, ``PAW_CF_DEPLOY_MODE`` alone
#   flipped 89 tests red (98 failed / 909 passed, against 5 / 1005 on a clean tree) by
#   sending them down the real Cloudflare Workers path with the operator's live API
#   token in the environment. CI never catches it (no ``.env`` there — green by
#   construction), so it only bites locally, where "tests broken" reads as "branch
#   broken". This deletes the variables that SELECT a code path or SUPPLY a credential
#   before every test. Delete-only and function-scoped on purpose: a test that wants one
#   set does so in its own body, after this has run. Deleting alone was NOT enough —
#   ``pocketpaw.uploads.factory.build_adapter`` calls ``load_dotenv()`` lazily, so it
#   re-read the file mid-test and put the variables back (a non-overriding load happily
#   sets a var this fixture has just removed); three test_preview_refresh.py tests were
#   still reaching the live path. So the fixture also neuters ``load_dotenv`` per test.
#   test_env_isolation.py proves both legs, so neither can quietly stop working.
# Updated 2026-08-08 (capture readiness): added the autouse ``_site_pages_are_serving``
#   fixture. Screenshot capture now polls the site's url before rendering it, so
#   without this every existing capture test would fire a real request at a hostname
#   that does not resolve and then wait out the retry schedule. It defaults the probe
#   to ready and zeroes both delay schedules; test_capture_readiness.py patches the
#   same attributes to exercise the gate itself.
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


# The ambient variables an operator's own ``.env`` leaks into the test process. Each one
# either SELECTS a code path (``PAW_CF_DEPLOY_MODE`` picks the real Workers deploy over
# the local one), SUPPLIES a credential that path would then use for real, or overrides a
# default the tests assert against (``PAW_CF_SITES_DOMAIN``, ``PAW_CF_WRANGLER_CMD``).
# The whole family is listed rather than only the variables observed failing: CI runs
# with none of them set, so anything green there is green with all of them absent, which
# makes deleting the rest free and stops the same leak reappearing through a sibling
# variable. Kept module-level so test_env_isolation.py asserts against this exact list
# instead of a copy that can drift.
OPERATOR_ENV_VARS: tuple[str, ...] = (
    "PAW_CF_DEPLOY_MODE",
    "PAW_CF_ZONE_ID",
    "PAW_CF_ACCOUNT_ID",
    "PAW_CF_API_TOKEN",
    "PAW_CF_CNAME_TARGET",
    "PAW_CF_WORKERS_SUBDOMAIN",
    "PAW_CF_DISPATCH_NAMESPACE",
    "PAW_CF_SITES_DOMAIN",
    "PAW_CF_WRANGLER_CMD",
    "PAW_CF_MIGRATE_TIMEOUT_SEC",
    "DAYTONA_API_KEY",
    "DAYTONA_API_URL",
)


@pytest.fixture(autouse=True)
def _operator_env_cleared(monkeypatch):
    """Remove the operator's own Cloudflare / Daytona environment from every sites test
    (fix/tests-ignore-operator-env).

    ``config.py`` sets ``env_file=".env"`` and ``security/url_validators.py`` calls
    ``load_dotenv(override=False)`` at import time, which searches UPWARD from the CWD.
    So a gitignored ``.env`` in the checkout root is test input, and the suite's result
    depends on who is running it: with one present ``PAW_CF_DEPLOY_MODE`` alone took the
    tree from 5 failures to 98, because the deploy tests took the real Workers path with
    a live API token sitting in the environment. The observed failures died early on a
    missing build output, but nothing structural was stopping a less-mocked test from
    reaching Cloudflare for real.

    Deleting, not setting, is the point: it makes each test's behaviour a property of
    the code under test rather than of the machine. Function-scoped so a test that WANTS
    one of these sets it in its own body, after this fixture has already run —
    test_service.py, test_fault_ladder_deploy.py and test_workers_deploy.py all do
    exactly that, and are unaffected.

    DELETING IS NOT ENOUGH ON ITS OWN, which is the part worth remembering. Some call
    sites load the file LAZILY, inside the function, so they re-read it DURING the test —
    ``pocketpaw.uploads.factory.build_adapter`` is the one that bit here (its own comment
    reasons that ``load_dotenv`` "won't override vars already in env", which is true and
    beside the point: this fixture has just made the var ABSENT, so a non-overriding load
    is free to set it again). Three of test_preview_refresh.py's tests store a screenshot,
    reach ``build_adapter``, and were still taking the live Workers path with the
    variables deleted. So the second leg neuters ``load_dotenv`` for the duration of each
    test: the ambient file is not test input, whenever it is read from."""
    for name in OPERATOR_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    def _refuse_to_load_dotenv(*_args: Any, **_kwargs: Any) -> bool:
        """Stand in for ``dotenv.load_dotenv``. Returns False, its "loaded nothing"
        value, so a caller that checks the result sees an empty file rather than an
        error."""
        return False

    try:
        import dotenv
        import dotenv.main
    except Exception:  # pragma: no cover — dotenv is only an indirect dep
        pass
    else:
        # Both spellings: callers use ``from dotenv import load_dotenv`` today, and the
        # lazy import resolves the attribute at CALL time, so patching the module object
        # is what catches them. ``dotenv.main`` is patched too so a future
        # ``from dotenv.main import load_dotenv`` cannot quietly reopen the hole.
        monkeypatch.setattr(dotenv, "load_dotenv", _refuse_to_load_dotenv)
        monkeypatch.setattr(dotenv.main, "load_dotenv", _refuse_to_load_dotenv)
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
def _site_pages_are_serving(monkeypatch):
    """Default the screenshot readiness probe to "the page is up", with no waits.

    A capture now polls the site's own url before spending a Browser Rendering call
    (a deploy is live at Cloudflare before it is live at the edge, and a screenshot
    of the 404 in between is a valid PNG that lands on the card forever). Left
    un-stubbed, every existing capture test would make a REAL request to
    ``brew.example.test`` / ``*.paw-sites.test``, get nothing, and then sit through
    the retry schedule before failing — slow, flaky, and offline-hostile.

    Defaulting the gate OPEN is the right default here: these tests are about what
    happens once there is a page. The tests that are about the GATE
    (test_capture_readiness.py) patch the same two attributes themselves, and an
    inner patch of the same target wins while it is active."""
    from pocketpaw_ee.sites import screenshot as screenshot_mod

    async def _serving(_url: str, **_kw) -> bool:
        return True

    monkeypatch.setattr(screenshot_mod, "_url_is_serving", _serving)
    monkeypatch.setattr(screenshot_mod, "_READY_DELAYS", ())
    monkeypatch.setattr(screenshot_mod, "_READY_DELAYS_MANUAL", ())
    yield


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
