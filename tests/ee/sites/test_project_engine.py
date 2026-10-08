# Tests for the ``project`` engine (the author owns the whole repo): its engine
# predicates, the sandbox draft build (Daytona and the paw-sites CLI are faked), the
# build log endpoints (auth + redaction), publish through ``bundle_deploy`` with the
# stored manifest, the free-plan gate, and ``preview_mode``.
from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bson import ObjectId
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.errors import (
    CloudError,
    ConflictError,
    Forbidden,
    NotFound,
    ValidationError,
)
from pocketpaw_ee.cloud.models.pocket import Pocket
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.sites import build_job, engines, project_build, verify_store
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.daytona_build import BuildClassification, build_wrapper_script
from pocketpaw_ee.sites.daytona_runner import BuildRunResult, BuildTimings

SOURCE = {
    "package.json": json.dumps({"name": "x", "scripts": {"build": "astro build"}}),
    "astro.config.mjs": "export default {};\n",
    "src/pages/index.astro": "<h1>Hi</h1>\n",
}

STATIC_MANIFEST = {
    "slug": "x",
    "assetsDir": "dist",
    "workerModules": [],
    "compat": {"date": "2026-09-01", "flags": []},
    "bindingRequests": [],
    "framework": "astro",
    "sizes": {"assetFiles": 1, "assetBytes": 10, "workerBytes": 0},
}

WORKER_MANIFEST = {
    "slug": "x",
    "assetsDir": "dist/client",
    "workerEntry": ".paw/worker/index.js",
    "workerModuleDir": ".paw/worker",
    "mainModule": "index.js",
    "workerModules": [".paw/worker/index.js"],
    "compat": {"date": "2026-09-01", "flags": ["nodejs_compat"]},
    "bindingRequests": [{"type": "d1", "name": "DB"}, {"type": "kv", "name": "CACHE"}],
    "framework": "astro",
}


def _tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"./{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _bundle(manifest: dict, *, index: bool = True, assets: dict | None = None) -> bytes:
    """A staged bundle. ``index=False`` leaves out the root index.html, as a worker
    template (next, sveltekit) that renders every page on the server stages it."""
    root = manifest["assetsDir"]
    files = {
        "paw-build.json": json.dumps(manifest).encode(),
        f"{root}/_headers": b"/*\n  X-Test: 1\n",
    }
    if index:
        files[f"{root}/index.html"] = b"<h1>Hi</h1>"
    for rel, data in (assets or {}).items():
        files[f"{root}/{rel}"] = data
    for module in manifest.get("workerModules") or []:
        files[module] = b"export default { fetch() { return new Response('ok') } }"
    return _tar(files)


def _result(*, artifact: bytes | None, outcome: str = "completed_ok", tail: str = ""):
    return BuildRunResult(
        classification=BuildClassification(
            outcome=outcome,  # type: ignore[arg-type]
            reason="ok" if outcome == "completed_ok" else "build_failed",
            retryable=False,
            blames_user=outcome == "build_failed",
            stderr_tail=tail,
        ),
        timings=BuildTimings(0, 0, 0, 0, 0),
        artifact=artifact,
        artifact_bytes=len(artifact or b""),
        sandbox_id="sb1",
        sandbox_deleted=True,
    )


class _Runner:
    def __init__(self, result: BuildRunResult) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, files, **kw):
        self.calls.append({"files": files, **kw})
        return self.result


class _Store:
    """Artifact store with the draft-preview surface, in memory."""

    def __init__(self) -> None:
        self.dist: dict[tuple[str, str], bytes] = {}
        self.tokens: dict[tuple[str, str], str] = {}

    def read(self, pocket_id, content_hash):
        return None

    def write(self, pocket_id, content_hash, body_html, css):
        pass

    def write_dist(self, pocket_id, content_hash, data):
        self.dist[(pocket_id, content_hash)] = data
        return True

    def read_dist(self, pocket_id, content_hash):
        return self.dist.get((pocket_id, content_hash))

    def write_preview_token(self, pocket_id, content_hash, token):
        self.tokens[(pocket_id, content_hash)] = token
        return True

    def read_preview_token(self, pocket_id, content_hash):
        return self.tokens.get((pocket_id, content_hash))

    def resolve_preview_token(self, token):
        return None


class _Records:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], dict] = {}

    def read(self, pocket_id, key):
        return self.data.get((pocket_id, key))

    def write(self, pocket_id, key, record):
        self.data[(pocket_id, key)] = json.loads(json.dumps(record))


class _Pool:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def enqueue_job(self, function, *args, _job_id=None, **kw):
        self.calls.append({"function": function, "args": args, "job_id": _job_id})
        return object()


GEN = [(b"{}", "/tmp/paw-sites-gen/package.json"), (b"//cli", "/tmp/paw-sites-gen/dist/cli.js")]


@pytest.fixture(autouse=True)
def _preview_base(monkeypatch):
    monkeypatch.setenv("PAW_SITES_PREVIEW_BASE_URL", "https://preview.paw.test")


# ---------------------------------------------------------------- predicates


def test_project_is_a_known_engine_with_its_own_capabilities():
    assert engines.normalize_engine("project") == "project"
    assert engines.is_source_engine("project")
    assert engines.content_key("project") == "source"
    assert engines.needs_node_build("project")
    assert engines.takes_author_packages("project")
    assert engines.build_requires_sandbox("project")
    assert not engines.has_generator_scaffold("project")
    assert not engines.has_native_edit_lane("project")
    assert not engines.has_write_back_lane("project")
    assert engines.expects_server_worker("project") is None
    assert engines.static_output_rel("project") == engines.PROJECT_STAGE_REL
    assert engines.engine_capabilities("project") == {
        "select": False,
        "text": False,
        "code": True,
        "build_log": True,
    }


@pytest.mark.parametrize("engine", ["ripple", "svelte", "html", "react"])
def test_scaffold_engines_keep_their_answers(engine):
    assert engines.has_generator_scaffold(engine)
    assert not engines.takes_author_packages(engine)
    assert not engines.build_requires_sandbox(engine)
    assert engines.engine_capabilities(engine)["build_log"] is False


def test_project_is_buildable_in_the_lane():
    assert "project" in build_job.BUILDABLE_ENGINES
    assert build_job.is_buildable_engine("project")


def test_resolvers_read_the_manifest(tmp_path: Path):
    (tmp_path / "paw-build.json").write_text(json.dumps(WORKER_MANIFEST))
    assert engines.resolve_static_output_rel(tmp_path, "project") == "dist/client"
    assert engines.resolve_emits_server_worker(tmp_path, "project") is True

    staged = tmp_path / "proj"
    (staged / ".paw" / "out").mkdir(parents=True)
    (staged / ".paw" / "out" / "paw-build.json").write_text(json.dumps(STATIC_MANIFEST))
    assert engines.resolve_static_output_rel(staged, "project") == ".paw/out/dist"
    assert engines.resolve_emits_server_worker(staged, "project") is False


def test_a_manifest_path_that_escapes_is_ignored(tmp_path: Path):
    (tmp_path / "paw-build.json").write_text(json.dumps({"assetsDir": "../../etc"}))
    assert engines.resolve_static_output_rel(tmp_path, "project") == engines.PROJECT_STAGE_REL


def test_project_wrapper_tars_the_stage_dir():
    script = build_wrapper_script(
        "project",
        "/home/daytona/paw-build",
        timeout_seconds=600,
        artifact_path="/tmp/a.tgz",
        install_command="true",
        build_command="bash /tmp/paw-project-build.sh",
        artifact_rel=engines.PROJECT_STAGE_REL,
        ssr_markers=(),
        log_tail_bytes=project_build.LOG_CAP_BYTES,
    )
    assert "-C /home/daytona/paw-build/.paw/out" in script
    assert "fh.seek(-65536" in script
    assert "known SSR failure marker" not in script


def test_host_builds_are_refused():
    import asyncio

    from pocketpaw_ee.sites.generator_client import GeneratorClient, HostInstallRefused

    with pytest.raises(HostInstallRefused):
        asyncio.run(
            GeneratorClient()._build_one(
                ripple_spec=None,
                theme={},
                site_id="s",
                title="t",
                capture_api_base="",
                capture_signed_key="",
                engine="project",
                source={"src/index.ts": "x"},
                builder_origin=None,
                d1_database_id="",
                keeps_client_bundle=False,
                pocket_id=None,
                smoke=False,
            )
        )


# ----------------------------------------------------------------- build lane


@pytest.mark.asyncio
async def test_clean_build_stores_bundle_preview_and_record():
    store, records = _Store(), _Records()
    runner = _Runner(_result(artifact=_bundle(STATIC_MANIFEST), tail="installing...\nbuilt\n"))
    out = await project_build.run_project_preview_build(
        {},
        "pk1",
        "h1",
        {"source": SOURCE},
        600,
        _runner=runner,
        _store=store,
        _verify_store=records,
        _gen_uploads=GEN,
    )

    assert out["status"] == "built"
    assert out["preview_mode"] == "full"
    call = runner.calls[0]
    assert call["engine"] == "project"
    assert call["install_command"] == "true"
    assert call["build_command"] == f"bash {project_build.SANDBOX_BUILD_SCRIPT}"
    assert call["artifact_rel"] == engines.PROJECT_STAGE_REL
    assert call["ssr_markers"] == ()
    assert set(call["files"]) == set(SOURCE)
    paths = [p for _b, p in call["extra_uploads"]]
    assert "/tmp/paw-sites-gen/dist/cli.js" in paths
    assert project_build.SANDBOX_BUILD_SCRIPT in paths
    assert project_build.SANDBOX_STAGE_SCRIPT in paths
    script = dict((p, b) for b, p in call["extra_uploads"])[project_build.SANDBOX_BUILD_SCRIPT]
    assert b"project-build --dir /home/daytona/paw-build --out" in script

    # The whole bundle is kept for publish; the preview gets the assets only.
    assert store.dist[("pk1", "h1.bundle")] == runner.result.artifact
    from pocketpaw_ee.sites import preview_origin

    preview = preview_origin.unpack_files(store.dist[("pk1", "h1")])
    assert set(preview) == {"index.html"}
    assert ("pk1", "h1") in store.tokens

    job_id = build_job._preview_job_id("pk1", "h1")
    record = records.read("pk1", project_build.build_record_key(job_id))
    assert record["status"] == "built"
    assert record["preview_mode"] == "full"
    assert record["framework"] == "astro"
    assert "built" in record["log"]
    latest = records.read("pk1", verify_store.LATEST_BUILD_KEY)
    assert latest["job_id"] == job_id and "log" not in latest


@pytest.mark.asyncio
async def test_worker_build_previews_static():
    store, records = _Store(), _Records()
    runner = _Runner(_result(artifact=_bundle(WORKER_MANIFEST)))
    out = await project_build.run_project_preview_build(
        {},
        "pk1",
        "h2",
        {"source": SOURCE},
        600,
        _runner=runner,
        _store=store,
        _verify_store=records,
        _gen_uploads=GEN,
    )
    assert out["status"] == "built"
    assert out["preview_mode"] == "static"
    from pocketpaw_ee.sites import preview_origin

    # The worker module never reaches the preview origin.
    assert set(preview_origin.unpack_files(store.dist[("pk1", "h2")])) == {"index.html"}


@pytest.mark.asyncio
async def test_failed_build_records_a_redacted_log_and_the_cli_code():
    store, records = _Store(), _Records()
    secret = "ghp_" + "A1b2C3d4" * 5
    tail = (
        "$ bun install\n"
        f"error at /home/daytona/paw-build/src/x.ts: token {secret}\n"
        '{"error":"build exited 1","code":"build_failed","log":"..."}\n'
    )
    runner = _Runner(_result(artifact=None, outcome="build_failed", tail=tail))
    out = await project_build.run_project_preview_build(
        {},
        "pk1",
        "h3",
        {"source": SOURCE},
        600,
        _runner=runner,
        _store=store,
        _verify_store=records,
        _gen_uploads=GEN,
    )
    assert out["status"] == "failed"
    assert out["reason"] == "build_failed:build_failed"
    assert store.dist == {}
    record = records.read(
        "pk1", project_build.build_record_key(build_job._preview_job_id("pk1", "h3"))
    )
    assert secret not in record["log"]
    assert "/home/daytona" not in record["log"]
    assert "src/x.ts" in record["log"]
    assert record["log"].count("\n") >= 2  # line structure survives


@pytest.mark.asyncio
async def test_missing_generator_and_bad_source_never_spend_a_sandbox(monkeypatch, tmp_path):
    monkeypatch.setenv(project_build.GEN_DIR_ENV, str(tmp_path / "no-generator-here"))
    records = _Records()
    runner = _Runner(_result(artifact=None))
    out = await project_build.run_project_preview_build(
        {}, "pk1", "h4", {"source": SOURCE}, 600, _runner=runner, _store=_Store(),
        _verify_store=records,
    )  # fmt: skip
    assert out == {
        "status": "failed",
        "reason": "sandbox_unavailable:generator_missing",
        "job_id": build_job._preview_job_id("pk1", "h4"),
        "preview_mode": None,
    }
    out = await project_build.run_project_preview_build(
        {}, "pk1", "h5", {"source": {"../x": "y"}}, 600, _runner=runner, _store=_Store(),
        _verify_store=records, _gen_uploads=GEN,
    )  # fmt: skip
    assert out["status"] == "failed"
    assert out["reason"] == "scaffold_failed:sites.project_bad_path"
    assert runner.calls == []


def test_generator_uploads_ship_dist_and_package_json(monkeypatch, tmp_path):
    (tmp_path / "dist" / "lib").mkdir(parents=True)
    (tmp_path / "dist" / "cli.js").write_text("// cli")
    (tmp_path / "dist" / "lib" / "a.js").write_text("// a")
    (tmp_path / "dist" / "cli.js.map").write_text("{}")
    (tmp_path / "package.json").write_text("{}")
    monkeypatch.setenv(project_build.GEN_DIR_ENV, str(tmp_path))
    paths = sorted(p for _b, p in project_build.generator_uploads())
    assert paths == [
        "/tmp/paw-sites-gen/dist/cli.js",
        "/tmp/paw-sites-gen/dist/lib/a.js",
        "/tmp/paw-sites-gen/package.json",
    ]


def test_cli_failure_code_reads_only_known_codes():
    output = 'step log\n{"error":"e","code":"wrangler_failed"}\n'
    assert project_build.cli_failure_code(output) == "wrangler_failed"
    # paw-sites refuses a Durable Objects config it will not deploy with ``do_config``.
    assert project_build.cli_failure_code('{"error":"e","code":"do_config"}') == "do_config"
    assert project_build.cli_failure_code('{"error":"e","code":"made_up"}') is None
    assert project_build.cli_failure_code("no json here") is None


@pytest.mark.asyncio
async def test_preview_job_dispatches_project_to_its_lane(monkeypatch):
    seen: dict = {}

    async def _fake(ctx, pocket_id, content_hash, generator_input, timeout_seconds, **kw):
        seen.update(pocket_id=pocket_id, content_hash=content_hash)
        return {"status": "built"}

    monkeypatch.setattr(project_build, "run_project_preview_build", _fake)
    out = await build_job.run_site_preview_build(
        {}, "pk9", "hh", {"source": SOURCE}, "project", 600
    )
    assert out == {"status": "built"}
    assert seen == {"pocket_id": "pk9", "content_hash": "hh"}


def test_log_redaction_caps_to_the_tail():
    text = "head-marker\n" + ("x" * 100 + "\n") * 2000 + "tail-marker\n"
    log, truncated = project_build.redact_build_log(text, cap=4096)
    assert truncated
    assert len(log.encode()) <= 4096
    assert "tail-marker" in log and "head-marker" not in log


# ------------------------------------------------------- native artifact + API


async def _make_project_pocket(user_id: str = "u1", workspace_id: str = "ws1") -> str:
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id=workspace_id,
        owner_id=user_id,
        name="Proj",
        type_="site",
        pattern="landing",
        ripple_spec=None,
        engine="project",
        source=dict(SOURCE),
        trusted=True,
    )
    assert err is None, err
    return pocket_id


@pytest.mark.asyncio
async def test_native_artifact_queues_then_serves_the_project_draft(beanie_test_db):
    pocket_id = await _make_project_pocket()
    store, records, pool = _Store(), _Records(), _Pool()

    cold = await sites_service._project_draft_artifact(
        pocket_id=pocket_id, source=dict(SOURCE), store=store, _pool=pool, _records=records
    )
    assert cold["build_status"] == "queued"
    assert cold["preview_mode"] is None
    assert cold["capabilities"] == engines.engine_capabilities("project")
    _pk, content_hash, payload, engine, _timeout = pool.calls[0]["args"]
    assert engine == "project"
    assert payload == {"source": SOURCE}
    assert content_hash == project_build.project_content_hash(SOURCE)
    assert records.read(pocket_id, verify_store.LATEST_BUILD_KEY)["status"] == "queued"

    # The worker lands.
    await project_build.run_project_preview_build(
        {}, pocket_id, content_hash, payload, 600,
        _runner=_Runner(_result(artifact=_bundle(WORKER_MANIFEST))),
        _store=store, _verify_store=records, _gen_uploads=GEN,
    )  # fmt: skip

    warm = await sites_service._project_draft_artifact(
        pocket_id=pocket_id, source=dict(SOURCE), store=store, _pool=pool, _records=records
    )
    assert warm["build_status"] == "none"
    assert warm["preview_mode"] == "static"
    assert warm["preview_url"].startswith("https://")
    assert len(pool.calls) == 1, "a served draft must not queue another sandbox"

    # The public entry point routes project pockets here and never arms them.
    via_route = await sites_service.get_native_artifact(
        workspace_id="ws1", user_id="u1", pocket_id=pocket_id, _store=store, _pool=pool
    )
    assert via_route["capabilities"]["build_log"] is True


@pytest.mark.asyncio
async def test_worker_project_draft_without_root_index_reports_unpreviewable(beanie_test_db):
    """A worker template that renders every page on the server (next, sveltekit)
    stages no root index.html. Its draft has nothing static to open, so no preview
    URL is handed out (it would 404) and the reason is said, not a bare 404."""
    pocket_id = await _make_project_pocket()
    store, records, pool = _Store(), _Records(), _Pool()
    content_hash = project_build.project_content_hash(SOURCE)

    out = await project_build.run_project_preview_build(
        {}, pocket_id, content_hash, {"source": SOURCE}, 600,
        _runner=_Runner(_result(artifact=_bundle(
            WORKER_MANIFEST, index=False, assets={"_build/app.js": b"export {}"}
        ))),
        _store=store, _verify_store=records, _gen_uploads=GEN,
    )  # fmt: skip
    assert out["status"] == "built"
    assert out["preview_mode"] == project_build.PREVIEW_SERVER_ONLY
    assert (pocket_id, content_hash) not in store.tokens

    art = await sites_service._project_draft_artifact(
        pocket_id=pocket_id, source=dict(SOURCE), store=store, _pool=pool, _records=records
    )
    assert art["build_status"] == "none"
    assert art["preview_url"] is None
    assert art["preview_mode"] == project_build.PREVIEW_SERVER_ONLY
    assert pool.calls == [], "an unpreviewable draft is not an evicted one: no rebuild"


@pytest.mark.asyncio
async def test_build_log_and_latest_are_scoped_to_the_pocket(beanie_test_db):
    pocket_id = await _make_project_pocket()
    records = _Records()
    content_hash = project_build.project_content_hash(SOURCE)
    job_id = build_job._preview_job_id(pocket_id, content_hash)
    project_build.write_build_record(
        records,
        pocket_id,
        project_build.new_record(job_id, content_hash, "failed", reason="build_failed:x", log="L"),
    )

    log = await sites_service.project_build_log(
        workspace_id="ws1", user_id="u1", pocket_id=pocket_id, job_id=job_id, _records=records
    )
    assert log["log"] == "L" and log["status"] == "failed"
    latest = await sites_service.project_latest_build(
        workspace_id="ws1", user_id="u1", pocket_id=pocket_id, _records=records
    )
    assert latest["job_id"] == job_id and latest["current"] is True

    # Another pocket's job id is a 404, and so is a missing record.
    with pytest.raises(NotFound):
        await sites_service.project_build_log(
            workspace_id="ws1",
            user_id="u1",
            pocket_id=pocket_id,
            job_id="site-preview-other-abc",
            _records=records,
        )
    # Another workspace cannot read it, even though the pocket is workspace-visible.
    with pytest.raises(NotFound):
        await sites_service.project_build_log(
            workspace_id="ws2", user_id="intruder", pocket_id=pocket_id, job_id=job_id,
            _records=records,
        )  # fmt: skip
    # A private pocket refuses a non-member in the same workspace.
    doc = await Pocket.get(ObjectId(pocket_id))
    await doc.set({"visibility": "private"})
    with pytest.raises((NotFound, Forbidden)):
        await sites_service.project_build_log(
            workspace_id="ws1", user_id="intruder", pocket_id=pocket_id, job_id=job_id,
            _records=records,
        )  # fmt: skip


@pytest.mark.asyncio
async def test_build_log_routes_require_fabric_write_and_return_the_shape(monkeypatch):
    from tests.ee.sites.test_router import _build_app

    async def _log(**kw):
        return {"pocket_id": kw["pocket_id"], "job_id": kw["job_id"], "status": "built",
                "log": "ok", "log_truncated": False, "preview_mode": "full"}  # fmt: skip

    async def _latest(**kw):
        return {"pocket_id": kw["pocket_id"], "job_id": "j1", "status": "building"}

    monkeypatch.setattr(sites_service, "project_build_log", _log)
    monkeypatch.setattr(sites_service, "project_latest_build", _latest)
    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/v1/sites/by-pocket/pk1/builds/site-preview-pk1-h/log")
        assert r.status_code == 200, r.text
        assert r.json()["log"] == "ok"
        r = await c.get("/api/v1/sites/by-pocket/pk1/builds/latest")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "building"

    from pocketpaw_ee.sites.router import router

    for path in (
        "/sites/by-pocket/{pocket_id}/builds/{job_id}/log",
        "/sites/by-pocket/{pocket_id}/builds/latest",
    ):
        route = next(r for r in router.routes if getattr(r, "path", "").endswith(path))
        deps = [repr(d.call) for d in route.dependant.dependencies]
        assert any("fabric.write" in d or "require_action" in d for d in deps), deps


# --------------------------------------------------------------------- publish


def test_plan_gate():
    worker = dict(WORKER_MANIFEST)
    project_build.check_plan_allows(STATIC_MANIFEST, paid=False, has_custom_domain=True)
    project_build.check_plan_allows(worker, paid=False, has_custom_domain=False)  # d1 + kv
    with pytest.raises(ValidationError, match="Site plan"):
        project_build.check_plan_allows(
            {**worker, "bindingRequests": [{"type": "r2", "name": "FILES"}]},
            paid=False,
            has_custom_domain=False,
        )
    with pytest.raises(ValidationError, match="custom domain"):
        project_build.check_plan_allows(worker, paid=False, has_custom_domain=True)
    project_build.check_plan_allows(
        {**worker, "bindingRequests": [{"type": "r2", "name": "FILES"}]},
        paid=True,
        has_custom_domain=True,
    )


def test_preview_mode():
    assert project_build.preview_mode(STATIC_MANIFEST) == "full"
    assert project_build.preview_mode(WORKER_MANIFEST) == "static"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["wfp", "workers"])
async def test_publish_deploys_the_stored_manifest(beanie_test_db, monkeypatch, mode):
    pocket_id = await _make_project_pocket()
    site_id = str(sites_service._live_object_id("ws1", pocket_id))
    store = _Store()
    content_hash = project_build.project_content_hash(SOURCE)
    store.dist[(pocket_id, project_build.bundle_key(content_hash))] = _bundle(WORKER_MANIFEST)
    monkeypatch.setattr(sites_service, "_default_artifact_store", lambda: store)
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", mode)

    calls: list[dict] = []

    async def _deploy_bundle(
        cf,
        *,
        script_name,
        build_dir,
        salt,
        provisioned=None,
        provision=None,
        before_upload=None,
        target="dispatch",
        paid=False,
    ):
        root = Path(build_dir)
        calls.append(
            {
                "script_name": script_name,
                "target": target,
                "paid": paid,
                "salt": salt,
                "manifest": json.loads((root / "paw-build.json").read_text()),
                "has_module": (root / ".paw/worker/index.js").is_file(),
                "headers_kept": (root / "dist/client/_headers").is_file(),
            }
        )
        return SimpleNamespace(script_name=script_name, modules=1, assets=1, warnings=[])

    from pocketpaw_ee.sites import bundle_deploy

    monkeypatch.setattr(bundle_deploy, "deploy_bundle", _deploy_bundle)

    async def _no_workers(*a, **k):
        raise AssertionError("a project must never deploy through wrangler")

    async def _enable(name):
        enabled.append(name)

    async def _subdomain():
        return "acct-sub"

    enabled: list[str] = []
    cf = SimpleNamespace(enable_workers_dev=_enable, workers_dev_subdomain=_subdomain)

    doc = await sites_service._deploy_site_doc(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        site_id=site_id,
        signed_key="k",
        site_name="Proj",
        ripple_spec=None,
        theme={},
        engine="project",
        source=dict(SOURCE),
        pattern="landing",
        cloudflare=cf,
        workers_deploy=_no_workers,
    )
    assert len(calls) == 1
    if mode == "wfp":
        # The WfP namespace, keyed on the bare site id, as before.
        assert calls[0]["target"] == "dispatch" and calls[0]["script_name"] == site_id
        assert enabled == [] and doc.deploy_target == "wfp"
    else:
        # Workers mode: an account-level Worker under the site's workers-mode name.
        name = calls[0]["script_name"]
        assert calls[0]["target"] == "account" and name == doc.worker_name
        assert enabled == [name] and doc.deploy_target == "workers"
        assert doc.url == f"https://{name}.acct-sub.workers.dev"
    assert calls[0]["salt"] == "ws1"
    # A site with no paid plan deploys under the free-tier CPU / subrequest caps.
    assert calls[0]["paid"] is False
    assert calls[0]["manifest"]["workerEntry"] == ".paw/worker/index.js"
    assert calls[0]["has_module"] and calls[0]["headers_kept"]
    assert doc.deployed is True


@pytest.mark.asyncio
async def test_publish_without_a_finished_build_is_a_409(beanie_test_db, monkeypatch):
    pocket_id = await _make_project_pocket()
    monkeypatch.setattr(sites_service, "_default_artifact_store", lambda: _Store())
    with pytest.raises(ConflictError):
        await sites_service._deploy_site_doc(
            workspace_id="ws1",
            user_id="u1",
            pocket_id=pocket_id,
            site_id=str(sites_service._live_object_id("ws1", pocket_id)),
            signed_key="k",
            site_name="Proj",
            ripple_spec=None,
            theme={},
            engine="project",
            source=dict(SOURCE),
        )


@pytest.mark.asyncio
async def test_publish_refuses_paid_bindings_on_free(beanie_test_db, monkeypatch):
    pocket_id = await _make_project_pocket()
    store = _Store()
    manifest = {**WORKER_MANIFEST, "bindingRequests": [{"type": "r2", "name": "FILES"}]}
    content_hash = project_build.project_content_hash(SOURCE)
    store.dist[(pocket_id, project_build.bundle_key(content_hash))] = _bundle(manifest)
    monkeypatch.setattr(sites_service, "_default_artifact_store", lambda: store)
    with pytest.raises(CloudError) as exc:
        await sites_service._deploy_site_doc(
            workspace_id="ws1",
            user_id="u1",
            pocket_id=pocket_id,
            site_id=str(sites_service._live_object_id("ws1", pocket_id)),
            signed_key="k",
            site_name="Proj",
            ripple_spec=None,
            theme={},
            engine="project",
            source=dict(SOURCE),
        )
    assert exc.value.code == "sites.server_code_not_entitled"


def _run_stage(project: Path) -> tuple[int, str]:
    import subprocess
    import sys

    script = project.parent / "stage.py"
    script.write_text(project_build.STAGE_SCRIPT, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(script), str(project), str(project / ".paw" / "out")],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_stage_script_copies_only_what_the_manifest_names(tmp_path: Path):
    project = tmp_path / "proj"
    (project / "dist" / "client").mkdir(parents=True)
    (project / "dist" / "client" / "index.html").write_text("<h1>Hi</h1>")
    (project / ".paw" / "worker").mkdir(parents=True)
    (project / ".paw" / "worker" / "index.js").write_text("export default {}")
    (project / "src").mkdir()
    (project / "src" / "secret.ts").write_text("source never ships")
    (project / "paw-build.json").write_text(json.dumps(WORKER_MANIFEST))

    code, output = _run_stage(project)
    assert code == 0, output
    out = project / ".paw" / "out"
    staged = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert staged == [".paw/worker/index.js", "dist/client/index.html", "paw-build.json"]
    assert engines.resolve_static_output_rel(out, "project") == "dist/client"


@pytest.mark.parametrize("assets_dir", ["../outside", ".", "/abs"])
def test_stage_script_refuses_paths_outside_the_project(tmp_path: Path, assets_dir: str):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "paw-build.json").write_text(
        json.dumps({**STATIC_MANIFEST, "assetsDir": assets_dir})
    )
    code, _output = _run_stage(project)
    assert code != 0
    assert not (project / ".paw" / "out").exists()
