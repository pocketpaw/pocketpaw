# tests/ee/sites/test_lazy_source_tags.py — a framework draft builds its source-tagged
# copy only while someone is editing it.
#
# Outside an edit session a sandbox draft builds once, untagged, under the source's
# content hash. An edit session (``POST /sites/by-pocket/{id}/edit-session``, renewed by
# every visual edit) moves the view to ``project_build.tagged_view_hash``: its build is
# tagged-only when the untagged bundle for the same source is already stored, else both.
# The publish bundle always lands under the base hash, so publish, verify and
# "is this build current" read the same key either way. The no-build lane stamps
# in-process and is never gated.
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from pocketpaw_ee.sites import (
    build_job,
    preview_origin,
    project_build,
    project_tools,
    verify_store,
    visual_edit,
)
from pocketpaw_ee.sites import service as sites_service

from tests.ee.sites.test_project_engine import (
    SOURCE,
    STATIC_MANIFEST,
    _bundle,
    _Pool,
    _Records,
    _result,
    _Runner,
    _Store,
    _tar,
)
from tests.ee.sites.test_project_files_routes import _app, _client, _pocket, _source
from tests.ee.sites.test_project_no_build import NOBUILD
from tests.ee.sites.test_visual_edit import _Proc

FULL_CLI = b'switch (cmd) { case "visual-edit": case "stamp-html": } // --source-tags'
GEN = [(b"{}", "/tmp/paw-sites-gen/package.json"), (FULL_CLI, "/tmp/paw-sites-gen/dist/cli.js")]
SESSION_URL = "/api/v1/sites/by-pocket/{pid}/edit-session"


@pytest.fixture(autouse=True)
def _preview_base(monkeypatch):
    monkeypatch.setenv("PAW_SITES_PREVIEW_BASE_URL", "https://preview.paw.test")
    monkeypatch.setenv(project_build.LAZY_TAGS_ENV, "1")


@pytest.fixture
def cli_text(monkeypatch):
    """The generator entry file the feature detection reads (all three features on)."""
    monkeypatch.setattr(visual_edit, "generator_cli_text", lambda: FULL_CLI)


@pytest.fixture
def cli(monkeypatch):
    """The paw-sites CLI at the subprocess seam: answers by subcommand."""
    state: dict = {"calls": [], "answers": {}}

    async def _exec(*argv, **kw):
        proc = _Proc(state["answers"].get(argv[-1], {}), 0)
        state["calls"].append(argv)
        return proc

    monkeypatch.setattr(project_tools, "_create_subprocess_exec", _exec)
    return state


@pytest.fixture
def stores(monkeypatch):
    store, records = _Store(), _Records()
    monkeypatch.setattr(sites_service, "_default_artifact_store", lambda: store)
    monkeypatch.setattr(verify_store, "default_verify_store", lambda: records)
    return store, records


@pytest.fixture
def enqueued(monkeypatch) -> list[dict]:
    """Every queued draft build's hash and input (no Redis)."""
    calls: list[dict] = []

    async def _enqueue(*, pocket_id, content_hash, engine, generator_input, **_kw):
        calls.append({"content_hash": content_hash, "input": generator_input})
        return SimpleNamespace(
            status="queued", job_id=f"site-preview-{pocket_id}-{content_hash}", reason=None
        )

    monkeypatch.setattr(build_job, "enqueue_preview_build", _enqueue)
    return calls


def _script(runner: _Runner) -> str:
    uploads = {p: b for b, p in runner.calls[0]["extra_uploads"]}
    return uploads[project_build.SANDBOX_BUILD_SCRIPT].decode()


def _builds(script: str) -> list[str]:
    return [line for line in script.splitlines() if "cli.js project-build" in line]


def _tagged_only_artifact() -> bytes:
    """A tagged-only build's stage dir: just the tagged copy under _paw_tagged/."""
    plain = preview_origin.unpack_files(_bundle(STATIC_MANIFEST))
    files = {}
    for name, data in plain.items():
        if name.endswith("index.html"):
            data = b'<h1 data-paw-src="src/pages/index.astro:1:1">Hi</h1>'
        files[f"{project_build.TAGGED_STAGE}/{name}"] = data
    return _tar(files)


def _record(records: _Records, pocket_id: str, content_hash: str) -> dict:
    job_id = build_job._preview_job_id(pocket_id, content_hash)
    return records.read(pocket_id, project_build.build_record_key(job_id))


# ------------------------------------------------------------------ the build job


async def test_a_draft_outside_an_edit_session_builds_once_untagged(cli_text):
    store, records = _Store(), _Records()
    runner = _Runner(_result(artifact=_bundle(STATIC_MANIFEST)))
    out = await project_build.run_project_preview_build(
        {}, "pk1", "h1", {"source": SOURCE}, 600,
        _runner=runner, _store=store, _verify_store=records, _gen_uploads=GEN,
    )  # fmt: skip
    assert out["status"] == "built"
    builds = _builds(_script(runner))
    assert len(builds) == 1
    assert "--source-tags" not in builds[0]
    assert store.dist[("pk1", project_build.bundle_key("h1"))] == _bundle(STATIC_MANIFEST)
    assert _record(records, "pk1", "h1")["source_tags"] is False


async def test_editing_with_the_bundle_stored_builds_only_the_tagged_copy(cli_text):
    store, records = _Store(), _Records()
    base, view = "h1", project_build.tagged_view_hash("h1")
    store.write_dist("pk1", project_build.bundle_key(base), _bundle(STATIC_MANIFEST))
    runner = _Runner(_result(artifact=_tagged_only_artifact()))
    out = await project_build.run_project_preview_build(
        {}, "pk1", view, {"source": SOURCE, "sourceTags": "only", "bundleHash": base}, 600,
        _runner=runner, _store=store, _verify_store=records, _gen_uploads=GEN,
    )  # fmt: skip
    assert out["status"] == "built"
    builds = _builds(_script(runner))
    assert len(builds) == 1
    assert "--source-tags" in builds[0]
    # The publish bundle is the stored untagged one, untouched; none under the view.
    assert store.dist[("pk1", project_build.bundle_key(base))] == _bundle(STATIC_MANIFEST)
    assert ("pk1", project_build.bundle_key(view)) not in store.dist
    preview = preview_origin.unpack_files(store.dist[("pk1", view)])
    assert b"data-paw-src" in preview["index.html"]
    assert _record(records, "pk1", view)["source_tags"] is True


async def test_a_failed_tagged_only_build_previews_the_stored_bundle(cli_text):
    store, records = _Store(), _Records()
    base, view = "h1", project_build.tagged_view_hash("h1")
    store.write_dist("pk1", project_build.bundle_key(base), _bundle(STATIC_MANIFEST))
    runner = _Runner(_result(artifact=_tar({})))
    out = await project_build.run_project_preview_build(
        {}, "pk1", view, {"source": SOURCE, "sourceTags": "only", "bundleHash": base}, 600,
        _runner=runner, _store=store, _verify_store=records, _gen_uploads=GEN,
    )  # fmt: skip
    assert out["status"] == "built"
    preview = preview_origin.unpack_files(store.dist[("pk1", view)])
    assert b"data-paw-src" not in preview["index.html"]
    assert _record(records, "pk1", view)["source_tags"] is False


async def test_editing_without_a_stored_bundle_builds_both_and_files_the_bundle_by_source(
    cli_text,
):
    store, records = _Store(), _Records()
    base, view = "h1", project_build.tagged_view_hash("h1")
    plain = preview_origin.unpack_files(_bundle(STATIC_MANIFEST))
    both = dict(plain)
    both.update({f"{project_build.TAGGED_STAGE}/{n}": d for n, d in plain.items()})
    runner = _Runner(_result(artifact=_tar(both)))
    await project_build.run_project_preview_build(
        {}, "pk1", view, {"source": SOURCE, "sourceTags": "both", "bundleHash": base}, 600,
        _runner=runner, _store=store, _verify_store=records, _gen_uploads=GEN,
    )  # fmt: skip
    builds = _builds(_script(runner))
    assert len(builds) == 2 and "--source-tags" in builds[1]
    bundle = preview_origin.unpack_files(store.dist[("pk1", project_build.bundle_key(base))])
    assert not any(n.startswith(project_build.TAGGED_STAGE) for n in bundle)
    assert ("pk1", project_build.bundle_key(view)) not in store.dist


# ------------------------------------------------------------- the edit session


def test_an_edit_session_expires():
    records = _Records()
    now = time.time()
    assert not project_build.edit_session_active(records, "pk1", now=now)
    expires = project_build.mark_edit_session(records, "pk1", now=now)
    assert expires == now + project_build.EDIT_SESSION_TTL_SECONDS
    assert project_build.edit_session_active(records, "pk1", now=now + 60)
    assert not project_build.edit_session_active(records, "pk1", now=expires + 1)
    project_build.mark_edit_session(records, "pk1", now=now)
    project_build.end_edit_session(records, "pk1")
    assert not project_build.edit_session_active(records, "pk1", now=now)


def test_the_session_record_is_never_evicted():
    assert verify_store.EDIT_SESSION_KEY in verify_store._POINTER_KEYS


def test_the_tagged_view_is_its_own_hash():
    view = project_build.tagged_view_hash("h1")
    assert view != "h1" and view == project_build.tagged_view_hash("h1")
    assert len(view) == 64 and int(view, 16) >= 0


# ---------------------------------------------------------------- the service


async def test_the_view_follows_the_edit_session(beanie_test_db, cli_text):
    pid = await _pocket()
    store, records, pool = _Store(), _Records(), _Pool()
    base = project_build.project_content_hash(SOURCE)

    await sites_service._project_draft_artifact(
        pocket_id=pid, source=dict(SOURCE), store=store, _pool=pool, _records=records
    )
    _pk, content_hash, payload, _engine, _t = pool.calls[0]["args"]
    assert content_hash == base
    assert payload == {"source": SOURCE}

    project_build.mark_edit_session(records, pid)
    out = await sites_service._project_draft_artifact(
        pocket_id=pid, source=dict(SOURCE), store=store, _pool=pool, _records=records
    )
    _pk, content_hash, payload, _engine, _t = pool.calls[1]["args"]
    assert content_hash == project_build.tagged_view_hash(base)
    assert payload["sourceTags"] == "both"  # nothing built yet for this source
    assert payload["bundleHash"] == base
    assert out["build_job_id"] == build_job._preview_job_id(pid, content_hash)


async def test_editing_a_built_draft_queues_only_the_tagged_copy(beanie_test_db, cli_text):
    pid = await _pocket()
    store, records, pool = _Store(), _Records(), _Pool()
    base = project_build.project_content_hash(SOURCE)
    await project_build.run_project_preview_build(
        {}, pid, base, {"source": SOURCE}, 600,
        _runner=_Runner(_result(artifact=_bundle(STATIC_MANIFEST))),
        _store=store, _verify_store=records, _gen_uploads=GEN,
    )  # fmt: skip
    project_build.mark_edit_session(records, pid)
    await sites_service._project_draft_artifact(
        pocket_id=pid, source=dict(SOURCE), store=store, _pool=pool, _records=records
    )
    _pk, content_hash, payload, _engine, _t = pool.calls[0]["args"]
    assert content_hash == project_build.tagged_view_hash(base)
    assert payload["sourceTags"] == "only"


async def test_after_the_session_a_tagged_build_is_served_not_rebuilt(beanie_test_db, cli_text):
    pid = await _pocket()
    store, records, pool = _Store(), _Records(), _Pool()
    base = project_build.project_content_hash(SOURCE)
    view = project_build.tagged_view_hash(base)
    plain = preview_origin.unpack_files(_bundle(STATIC_MANIFEST))
    both = {**plain, **{f"{project_build.TAGGED_STAGE}/{n}": d for n, d in plain.items()}}
    await project_build.run_project_preview_build(
        {}, pid, view, {"source": SOURCE, "sourceTags": "both", "bundleHash": base}, 600,
        _runner=_Runner(_result(artifact=_tar(both))),
        _store=store, _verify_store=records, _gen_uploads=GEN,
    )  # fmt: skip
    out = await sites_service._project_draft_artifact(
        pocket_id=pid, source=dict(SOURCE), store=store, _pool=pool, _records=records
    )
    assert out["build_status"] == "none"
    assert out["preview_url"].startswith("https://")
    assert pool.calls == []


async def test_with_the_gate_off_every_sandbox_draft_builds_both(
    beanie_test_db, cli_text, monkeypatch
):
    monkeypatch.delenv(project_build.LAZY_TAGS_ENV)
    pid = await _pocket()
    store, records, pool = _Store(), _Records(), _Pool()
    await sites_service._project_draft_artifact(
        pocket_id=pid, source=dict(SOURCE), store=store, _pool=pool, _records=records
    )
    _pk, content_hash, payload, _engine, _t = pool.calls[0]["args"]
    assert content_hash == project_build.project_content_hash(SOURCE)
    assert payload == {"source": SOURCE, "sourceTags": "both"}


async def test_a_no_build_draft_is_never_gated(beanie_test_db, cli, cli_text):
    cli["answers"]["stamp-html"] = {"files": {}}
    pid = await _pocket(source=NOBUILD)
    store, records, pool = _Store(), _Records(), _Pool()
    out = await sites_service._project_draft_artifact(
        pocket_id=pid, source=dict(NOBUILD), store=store, _pool=pool, _records=records
    )
    assert out["build_status"] == "none"
    assert out["build_job_id"] == build_job._preview_job_id(
        pid, project_build.project_content_hash(NOBUILD)
    )
    assert pool.calls == []


async def test_a_tagged_build_counts_as_current(beanie_test_db, cli_text, stores):
    pid = await _pocket()
    _store, records = stores
    base = project_build.project_content_hash(await _source(pid))
    view = project_build.tagged_view_hash(base)
    job_id = build_job._preview_job_id(pid, view)
    project_build.write_build_record(
        records, pid, project_build.new_record(job_id, view, project_build.STATUS_BUILT)
    )
    out = await sites_service.project_latest_build(workspace_id="ws1", user_id="u1", pocket_id=pid)
    assert out["current"] is True


# ------------------------------------------------------------------ the routes


async def test_starting_an_edit_session_queues_the_tagged_build(
    beanie_test_db, cli_text, stores, enqueued
):
    pid = await _pocket()
    _store, records = stores
    async with _client(_app()) as c:
        r = await c.post(SESSION_URL.format(pid=pid))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["editing"] is True
        assert body["expires_at"]
        assert body["build_status"] == "queued"
        assert project_build.edit_session_active(records, pid)
        base = project_build.project_content_hash(await _source(pid))
        assert enqueued[-1]["content_hash"] == project_build.tagged_view_hash(base)

        r = await c.delete(SESSION_URL.format(pid=pid))
        assert r.status_code == 200, r.text
        assert r.json() == {"pocket_id": pid, "editing": False}
        assert not project_build.edit_session_active(records, pid)


async def test_an_edit_session_needs_a_project_in_this_workspace(beanie_test_db, stores):
    other = await _pocket(workspace_id="ws2", owner_id="u2")
    html = await _pocket(engine="html", source={"index.html": "<h1>Hi</h1>"})
    async with _client(_app()) as c:
        assert (await c.post(SESSION_URL.format(pid=other))).status_code == 404
        assert (await c.post(SESSION_URL.format(pid=html))).status_code == 422


async def test_a_visual_edit_renews_the_session(beanie_test_db, cli, cli_text, stores, enqueued):
    app_tsx = 'export default function App() {\n  return <h1 className="t">Hello</h1>;\n}\n'
    pid = await _pocket(source={**SOURCE, "src/App.tsx": app_tsx})
    _store, records = stores
    cli["answers"]["visual-edit"] = {"ok": True, "content": app_tsx.replace("Hello", "Hi")}
    async with _client(_app()) as c:
        r = await c.post(
            f"/api/v1/sites/by-pocket/{pid}/visual-edit",
            json={"src": "src/App.tsx:2:10", "kind": "text", "value": "Hi"},
        )
    assert r.status_code == 200, r.text
    assert project_build.edit_session_active(records, pid)
    assert enqueued[-1]["input"]["sourceTags"] in ("both", "only")
