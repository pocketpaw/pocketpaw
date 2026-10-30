# test_codeproject_registry_index.py — the startup reconciler that retires the
# superseded CodeProject registry index.
#
# Created 2026-07-22 (fix/starter-project-collision). The starter-collision fix
# widened the registry key from (workspace, user, provider, repo) to that plus
# ``registry_key``, so two projects built from one starter template are two rows.
# Beanie only ever CREATES the indexes a model declares — it never drops one that
# disappeared — so the superseded four-column unique index survives in every
# existing deployment and would still reject that second row. The service-level
# fix therefore passes on mongomock and 500s in production unless the old index
# is actually dropped at boot.
#
# These tests cover the reconciler that does it, mirroring the invite-token
# reconciler's tests in workspace/test_invite_send_500_regression.py: it drops the
# legacy shape, no-ops when it is absent, and leaves the current index alone.
from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.shared.db import _drop_legacy_code_project_index

pytestmark = pytest.mark.usefixtures("mongo_db")

_LEGACY = "ws_user_provider_repo_unique"
_CURRENT = "ws_user_provider_repo_key_unique"
_LEGACY_KEYS = [("workspace_id", 1), ("user_id", 1), ("provider", 1), ("repo", 1)]
_CURRENT_KEYS = [*_LEGACY_KEYS, ("registry_key", 1)]


async def test_drops_the_superseded_registry_index(mongo_db) -> None:
    """A deployment created before the fix carries the four-column unique index.

    Left in place it re-imposes exactly the constraint the fix removed, so the
    reconciler has to drop it.
    """
    coll = mongo_db["code_projects"]
    await coll.create_index(_LEGACY_KEYS, unique=True, name=_LEGACY)
    assert _LEGACY in await coll.index_information()

    await _drop_legacy_code_project_index(mongo_db)

    assert _LEGACY not in await coll.index_information()


async def test_is_a_noop_on_a_fresh_deployment(mongo_db) -> None:
    """No legacy index → nothing to reconcile, and nothing else is touched.

    The ``mongo_db`` fixture runs ``init_beanie``, so the collection already
    carries the model's own indexes and nothing else — which is precisely the
    state of a deployment created after this fix.
    """
    coll = mongo_db["code_projects"]
    before = set(await coll.index_information())
    assert _LEGACY not in before
    assert _CURRENT in before, "Beanie should have created the model's registry index"

    await _drop_legacy_code_project_index(mongo_db)

    assert set(await coll.index_information()) == before


async def test_preserves_the_current_registry_index(mongo_db) -> None:
    """Mid-upgrade both indexes exist. Only the superseded one may go — dropping
    the current one would leave the registry with no uniqueness at all.

    The current index here is the real one Beanie built from the model, not a
    replica, so this also pins that the two are distinguishable by name.
    """
    coll = mongo_db["code_projects"]
    await coll.create_index(_LEGACY_KEYS, unique=True, name=_LEGACY)

    await _drop_legacy_code_project_index(mongo_db)

    info = await coll.index_information()
    assert _LEGACY not in info
    assert _CURRENT in info


async def test_leaves_a_non_unique_index_of_the_same_name_alone(mongo_db) -> None:
    """The reconciler matches on shape, not just on name.

    An index that reuses the name without being the legacy unique constraint is
    somebody else's, and dropping it is not this function's business.
    """
    coll = mongo_db["code_projects"]
    await coll.create_index(_LEGACY_KEYS, name=_LEGACY)  # not unique

    await _drop_legacy_code_project_index(mongo_db)

    assert _LEGACY in await coll.index_information()
