# conftest.py — shared fixtures for the cloud/uploads test package.
# 2026-08-29 (T2): added the autouse ``no_real_transcription`` fixture. It pins
#   both halves of the fal seam, for two different reasons.
#   ``transcription._api_key`` resolves through ``studio.fal_edit.fal_api_key``,
#   which calls ``load_dotenv()`` — so on a developer box it returns the
#   workspace's REAL fal key, exactly the way ``build_adapter`` returns a real
#   S3 adapter (see the T0 note below; that one already wrote to a live bucket
#   once). It is pinned to an obvious fake so the gates downstream of it still
#   run.
#   ``transcription._call_fal`` is pinned to a stub that RAISES, not to one that
#   returns nothing. T0's fixture could safely answer ``None`` because that was
#   the pre-T0 behaviour; transcription has no pre-existing behaviour, so a
#   silent stub would let a test that accidentally reaches the network path
#   pass while proving nothing. A raise is loud, and the tests that mean to
#   exercise a transcription install their own fake over it.
# 2026-08-29 (T0): added the autouse ``no_real_storage_adapter`` fixture. It
#   pins ``uploads.extracted_text._resolve_adapter`` to ``None`` so a test that
#   forgets to inject an adapter falls back to re-extraction instead of
#   reaching the process-wide upload singleton. That singleton is NOT a
#   harmless local-disk stub on a developer box: ``build_adapter`` calls
#   ``load_dotenv()``, so a workspace ``.env`` carrying
#   ``POCKETPAW_UPLOAD_ADAPTER=s3`` makes it a LIVE S3 adapter pointed at a
#   real bucket — and a persist test then writes a real object over the
#   network. Observed for real while writing the T0 tests. Returning ``None``
#   is the same answer every pre-T0 test already got, so this changes nothing
#   about existing behaviour.
# 2026-07-03 (FL-6): the ``beanie_upload_db`` fixture now resets the Beanie
#   binding on ``FileUpload`` / ``FileFolder`` / ``ShareLink`` after each test.
#   Before FL-6 no test read Beanie state from inside the listener, so a leaked
#   binding (the torn-down mongomock db still attached to the Document classes)
#   was harmless. FL-6's listener loads the FileUpload row to honour
#   hide_from_ai, so a leaked binding let one test's seeded rows bleed into an
#   unrelated listener test (e.g. test_listener_vector), flipping the hide gate.
#   Clearing the per-class Beanie settings on teardown restores isolation.
# 2026-07-03 (FL-12b): ``beanie_upload_db`` also registers ``ShareLink`` so the
#   public share-link store/route tests get a real (mongomock) collection.
from __future__ import annotations

import uuid
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def no_real_storage_adapter(monkeypatch):
    """Never let an uploads test reach the process-wide storage singleton.

    See the module header: on a developer box that singleton can be a live S3
    adapter. Tests that mean to exercise the blob path inject their own
    adapter explicitly (``persist_extracted_text(..., adapter=...)``); every
    other test gets ``None``, which the reader treats as "re-extract" — the
    behaviour those tests already had before T0.

    NOT covered by ``tests/mutations/extracted_text.json``, deliberately: the
    mutation for this fixture is "make it inert", and running it would perform
    the real network write the fixture exists to prevent. It was verified by
    hand instead — before this fixture existed,
    ``test_persist_reports_failure_with_no_adapter`` returned ``True`` and left
    a real object in the dev bucket. That observation is the evidence.
    """
    from pocketpaw_ee.cloud.uploads import extracted_text

    monkeypatch.setattr(extracted_text, "_resolve_adapter", lambda: None)


#: What ``no_real_transcription`` hands to the key gate. Obviously not a key,
#: so a log line or an assertion carrying it is self-explaining.
FAKE_FAL_KEY = "test-fal-key-never-real"


@pytest.fixture(autouse=True)
def no_real_transcription(monkeypatch):
    """No uploads test may reach fal, and none may read the real fal key.

    See the module header. The key is faked rather than emptied so the ceiling
    and budget gates BELOW it still execute — a fixture that returned no key
    would short-circuit every test at gate 2 and quietly stop measuring the
    thing each one is named after.
    """
    from pocketpaw_ee.cloud.uploads import transcription

    async def _forbidden(*_a, **_kw):
        raise AssertionError(
            "a test reached the real fal transcription call. Install a fake "
            "over transcription._call_fal in the test that means to."
        )

    monkeypatch.setattr(transcription, "_api_key", lambda: FAKE_FAL_KEY)
    monkeypatch.setattr(transcription, "_call_fal", _forbidden)


@pytest.fixture()
def tmp_upload_root(tmp_path: Path) -> Path:
    """Isolated storage root for each test."""
    root = tmp_path / "uploads"
    root.mkdir()
    return root


@pytest.fixture()
async def beanie_upload_db():
    from beanie import init_beanie
    from mongomock_motor import AsyncMongoMockClient

    db_name = f"test_uploads_{uuid.uuid4().hex[:8]}"
    client = AsyncMongoMockClient()
    db = client[db_name]

    original = db.list_collection_names

    async def _safe(*_a, **_kw):
        return await original()

    db.list_collection_names = _safe  # type: ignore[method-assign]

    # Import after db creation to avoid circular imports
    from pocketpaw_ee.cloud.uploads.models import FileFolder, FileUpload
    from pocketpaw_ee.cloud.uploads.share_models import ShareLink

    await init_beanie(database=db, document_models=[FileUpload, FileFolder, ShareLink])
    try:
        yield db
    finally:
        # Reset the Beanie binding so this test's mongomock db (about to be
        # torn down) doesn't stay attached to the Document classes and leak
        # seeded rows into a later test that reads Beanie state (FL-6 listener
        # loads the FileUpload row). Beanie stores the collection on a private
        # settings object per Document; drop it so the next init rebinds clean.
        for model in (FileUpload, FileFolder, ShareLink):
            for attr in ("_document_settings", "_settings"):
                if hasattr(model, attr):
                    try:
                        setattr(model, attr, None)
                    except Exception:  # pragma: no cover — defensive
                        pass


@pytest.fixture()
async def store(beanie_upload_db):
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

    return MongoFileStore()


def install_workspace_caller(app) -> None:
    """Give a bare router-test app a caller the RBAC guards can resolve.

    The write routes carry ``require_action_any_workspace("uploads.write")``,
    whose first dep is fastapi-users' ``current_active_user``. A test app has no
    auth stack, so that resolved nothing and every write answered 401 before its
    role check ran — why 23 tests in this package were red on dev. Identity comes
    from the same x-user / x-workspace headers the other deps use; the guard and
    each route's own ACL still run for real.
    """
    from types import SimpleNamespace

    from fastapi import Header
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user

    add_error_handler(app)

    async def _active_user_dep(
        x_user: str = Header(default="u1"),
        x_workspace: str = Header(default="w1"),
    ):
        return SimpleNamespace(
            id=x_user,
            active_workspace=x_workspace,
            workspaces=[SimpleNamespace(workspace=x_workspace, role="owner")],
        )

    app.dependency_overrides[current_active_user] = _active_user_dep
