# test_codeproject_excluded_paths.py — node_modules never reaches blob storage.
#
# Created 2026-07-25 (fix/codeproject-never-store-generated-trees). Before this,
# the exclusion of the regenerable trees lived ONLY in the enumerating walks: the
# client's `listProjectFiles` and the VM's tar `--exclude`. Both are the paths that
# CHOOSE what to send. Nothing on the receiving side enforced it, so any caller
# that named a `node_modules` path — an editor save on a file the user opened in
# there, a client that forgot to filter, an older build — got it uploaded to the
# tenant's S3 and recorded on the project overlay.
#
# The rule these lock: the durable project store NEVER holds a regenerable tree,
# and the guarantee is the SERVER's, not the client's. Two entry points reach blob
# storage and both are covered:
#
#   • `mirror_file_to_project` (every single-file write, both runtimes) REJECTS
#     before spending a byte of storage. Loud, because a caller naming such a path
#     is a caller with a bug.
#   • `put_project_files` (the in-tab bulk write) SKIPS them. Not loud: its
#     contract is "here is my whole filesystem", so normalizing what the client
#     over-reported is the correct server behavior, not an error. Safe for the
#     prune too — the restore-time prune is already blind to excluded paths, so a
#     store that omits them never licenses deleting an installed node_modules.
from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.codeproject import service as codeproject_service
from pocketpaw_ee.cloud.websandbox import durability

from tests.cloud.test_codeproject_file_sync import _FakeUploads, _project

pytestmark = pytest.mark.usefixtures("mongo_db")

_WS = "w1"
_USER = "u1"

# One representative per reason the tree is excluded: installed dependencies,
# version-control internals, build output, framework and tool caches.
_EXCLUDED_WRITES = [
    "node_modules/left-pad/index.js",
    "node_modules/.package-lock.json",
    "packages/app/node_modules/dep/index.js",
    ".git/HEAD",
    "dist/bundle.js",
    "build/main.css",
    ".next/server/app.js",
    ".svelte-kit/generated/root.js",
    ".turbo/cache/x",
    ".cache/deps/y",
    "coverage/lcov.info",
]


# ── single-file writes reject, and spend nothing ─────────────────────────────


@pytest.mark.parametrize("rel_path", _EXCLUDED_WRITES)
async def test_a_single_file_write_to_a_generated_tree_is_refused(rel_path: str) -> None:
    project = await _project()
    uploads = _FakeUploads()

    with pytest.raises(CloudError) as excinfo:
        await durability.put_project_file(
            _WS, _USER, project.id, rel_path, "regenerable", uploads=uploads
        )

    assert excinfo.value.code == "codeproject.path_not_durable"
    # THE POINT: refused BEFORE the upload, so no byte of the tenant's storage was
    # spent and no overlay pointer was recorded.
    assert uploads.upload_calls == []
    view = await codeproject_service.get_project(_WS, _USER, project.id)
    assert view.overlay == {}


async def test_the_deep_mirror_chokepoint_refuses_too() -> None:
    """`mirror_file_to_project` is the ONE upload path both runtimes share, so the
    guard belongs there and not only at the wire boundary above it."""
    project = await _project()
    uploads = _FakeUploads()

    with pytest.raises(CloudError) as excinfo:
        await durability.mirror_file_to_project(
            _WS, _USER, project.id, "node_modules/dep/index.js", b"bytes", uploads=uploads
        )

    assert excinfo.value.code == "codeproject.path_not_durable"
    assert uploads.upload_calls == []


async def test_a_source_file_whose_NAME_collides_is_still_stored() -> None:
    """SEGMENT, not substring. `build/` is regenerable output; `build.ts` is source
    and must still persist, or this guard eats real work."""
    project = await _project()
    uploads = _FakeUploads()

    for rel_path in ("build.ts", "src/dist.ts", "coverage.md", "src/node_modules.md"):
        await durability.put_project_file(
            _WS, _USER, project.id, rel_path, "real source", uploads=uploads
        )

    view = await codeproject_service.get_project(_WS, _USER, project.id)
    assert set(view.overlay) == {"build.ts", "src/dist.ts", "coverage.md", "src/node_modules.md"}


# ── the bulk write normalizes instead of failing ─────────────────────────────


async def test_a_bulk_write_skips_generated_trees_and_keeps_the_rest() -> None:
    project = await _project()
    uploads = _FakeUploads()

    stored = await durability.put_project_files(
        _WS,
        _USER,
        project.id,
        {
            "src/App.tsx": "export default App;",
            "package.json": "{}",
            "node_modules/left-pad/index.js": "module.exports = pad;",
            "dist/bundle.js": "minified",
            ".git/HEAD": "ref: refs/heads/main",
        },
        uploads=uploads,
    )

    assert stored == 2
    view = await codeproject_service.get_project(_WS, _USER, project.id)
    assert set(view.overlay) == {"src/App.tsx", "package.json"}
    # Still a COMPLETE store: the prune is blind to excluded paths, so omitting
    # them cannot license deleting an installed node_modules.
    assert view.overlay_complete is True
    assert len(uploads.upload_calls) == 2


async def test_a_bulk_write_of_nothing_but_generated_trees_stores_nothing() -> None:
    """The degenerate case: a client that sent only noise leaves an empty store
    rather than an error, and still spends no storage."""
    project = await _project()
    uploads = _FakeUploads()

    stored = await durability.put_project_files(
        _WS,
        _USER,
        project.id,
        {"node_modules/a/index.js": "x", "dist/b.js": "y"},
        uploads=uploads,
    )

    assert stored == 0
    assert uploads.upload_calls == []
    view = await codeproject_service.get_project(_WS, _USER, project.id)
    assert view.overlay == {}


async def test_the_excluded_set_is_the_snapshot_set() -> None:
    """One definition of "regenerable", not two. The guard reuses the set the
    snapshot/prune already answers to, so the two cannot drift apart."""
    assert durability._is_excluded_snapshot_path("node_modules/x")
    assert not durability._is_excluded_snapshot_path("src/build.ts")
    assert "node_modules" in durability._SNAPSHOT_EXCLUDED_SEGMENTS
