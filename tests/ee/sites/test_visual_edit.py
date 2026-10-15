# tests/ee/sites/test_visual_edit.py — visual-edit write-back for project sites
# (``sites/visual_edit.py`` and its ``POST /sites/by-pocket/{id}/visual-edit`` route),
# against a mongomock pocket with the paw-sites CLI faked at the subprocess seam: src
# parsing and path refusal, value checks, each generator refusal's HTTP status, a
# success that saves through the project writer and queues the draft build, the
# version guard (501, capabilities off), draft-only ``--source-tags`` (the stored
# publish bundle stays untagged) and ``stamp-html`` on the no-build preview only.
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites import (
    build_job,
    engines,
    preview_origin,
    project_build,
    project_tools,
    visual_edit,
)

from tests.ee.sites.test_project_engine import (
    SOURCE,
    STATIC_MANIFEST,
    _bundle,
    _Records,
    _result,
    _Runner,
    _Store,
    _tar,
)
from tests.ee.sites.test_project_files_routes import _app, _client, _pocket, _source
from tests.ee.sites.test_project_no_build import NOBUILD

APP = 'export default function App() {\n  return <h1 className="t">Hello</h1>;\n}\n'
REACT_SOURCE = {**SOURCE, "src/App.tsx": APP}
URL = "/api/v1/sites/by-pocket/{pid}/visual-edit"
FULL_CLI = b'switch (cmd) { case "visual-edit": case "stamp-html": } // --source-tags'


@pytest.fixture(autouse=True)
def _preview_base(monkeypatch):
    monkeypatch.setenv("PAW_SITES_PREVIEW_BASE_URL", "https://preview.paw.test")


@pytest.fixture
def enqueued(monkeypatch) -> list[str]:
    """The content hash of every queued draft build (no Redis)."""
    hashes: list[str] = []

    async def _enqueue(*, pocket_id, content_hash, engine, generator_input, **_kw):
        hashes.append(content_hash)
        return SimpleNamespace(
            status="queued", job_id=f"site-preview-{pocket_id}-{content_hash}", reason=None
        )

    monkeypatch.setattr(build_job, "enqueue_preview_build", _enqueue)
    return hashes


@pytest.fixture
def cli_text(monkeypatch):
    """The generator entry file the feature detection reads (all three features on)."""
    state = {"text": FULL_CLI}
    monkeypatch.setattr(visual_edit, "generator_cli_text", lambda: state["text"])
    return state


class _Proc:
    def __init__(self, out: Any, code: int = 0) -> None:
        self.out = out
        self.returncode = code

    async def communicate(self, data: bytes | None = None):
        self.stdin = data
        body = self.out if isinstance(self.out, bytes) else json.dumps(self.out).encode()
        return body + b"\n", b"boom"


@pytest.fixture
def cli(monkeypatch):
    """The paw-sites CLI at the subprocess seam: answers by subcommand, records calls."""
    state: dict[str, Any] = {"calls": [], "answers": {}, "code": 0}

    async def _exec(*argv, **kw):
        command = argv[-1]
        proc = _Proc(state["answers"].get(command, {}), state["code"])
        state["calls"].append({"argv": argv, "kw": kw, "proc": proc})
        return proc

    monkeypatch.setattr(project_tools, "_create_subprocess_exec", _exec)
    return state


def _record(records: _Records, pocket_id: str, content_hash: str) -> dict:
    job_id = build_job._preview_job_id(pocket_id, content_hash)
    return records.data[(pocket_id, project_build.build_record_key(job_id))]


def _payload(call: dict) -> dict:
    return json.loads(call["proc"].stdin.decode())


# ---------------------------------------------------------------- input checks


def test_parse_src_reads_file_line_col():
    assert visual_edit.parse_src("src/App.tsx:2:10") == ("src/App.tsx", 2, 10)
    assert visual_edit.parse_src("./pages/a b.astro:1:1") == ("pages/a b.astro", 1, 1)


@pytest.mark.parametrize(
    "src",
    [
        "../secrets.tsx:1:1",
        "src/../../x.tsx:1:1",
        "/etc/passwd.html:1:1",
        "C:/x.html:1:1",
        "src\\App.tsx:1:1",
        "node_modules/a/index.jsx:1:1",
        ".paw/static/index.html:1:1",
        "src/App.ts:1:1",
        "src/data.json:1:1",
        "src/App.tsx:0:1",
        "src/App.tsx:1:x",
        "src/App.tsx:1",
        "src/App.tsx",
        "",
        None,
    ],
)
def test_parse_src_refuses_bad_or_escaping_paths(src):
    with pytest.raises(ValidationError):
        visual_edit.parse_src(src)


@pytest.mark.parametrize(
    ("kind", "value", "expected"),
    [
        ("text", "Hi", "Hi"),
        ("class", "p-4 text-lg", "p-4 text-lg"),
        ("attr", {"name": "href", "value": "/about"}, {"name": "href", "value": "/about"}),
        ("attr", {"name": "alt", "value": "A cat"}, {"name": "alt", "value": "A cat"}),
        ("delete", None, None),
        ("duplicate", None, None),
        ("move", "up", "up"),
        ("move", "down", "down"),
    ],
)
def test_check_value_accepts_the_contract(kind, value, expected):
    assert visual_edit.check_value(kind, value) == expected


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("text", 3),
        ("text", "x" * (visual_edit.MAX_TEXT_CHARS + 1)),
        ("class", None),
        ("attr", {"name": "onclick", "value": "x()"}),
        ("attr", {"name": "href", "value": "javascript:alert(1)"}),
        ("attr", {"name": "src", "value": " JavaScript:alert(1)"}),
        ("attr", "href"),
        ("delete", "x"),
        ("move", "left"),
        ("rewrite", None),
    ],
)
def test_check_value_refuses_off_contract(kind, value):
    with pytest.raises(ValidationError):
        visual_edit.check_value(kind, value)


# ------------------------------------------------------------------ the route


async def test_success_writes_through_the_project_writer_and_queues_the_build(
    beanie_test_db, enqueued, cli, cli_text
):
    pid = await _pocket(source=REACT_SOURCE)
    new = APP.replace("Hello", "Welcome")
    cli["answers"]["visual-edit"] = {"ok": True, "content": new}
    async with _client(_app()) as c:
        r = await c.post(
            URL.format(pid=pid),
            json={
                "src": "src/App.tsx:2:10",
                "kind": "text",
                "value": "Welcome",
                "base_hash": visual_edit.file_hash(APP),
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["changed"] is True
    assert body["file"] == "src/App.tsx"
    assert body["file_hash"] == visual_edit.file_hash(new)
    assert body["build_status"] == "pending"

    source = await _source(pid)
    assert source["src/App.tsx"] == new
    after = project_build.project_content_hash(source)
    assert enqueued == [after]
    assert body["job_id"] == f"site-preview-{pid}-{after}"

    (call,) = cli["calls"]
    assert call["argv"][-1] == "visual-edit"
    assert _payload(call) == {
        "file": "src/App.tsx",
        "content": APP,
        "src": "src/App.tsx:2:10",
        "kind": "text",
        "value": "Welcome",
    }


async def test_the_edit_lands_in_history_like_any_file_write(
    beanie_test_db, enqueued, cli, cli_text
):
    from bson import ObjectId
    from pocketpaw_ee.cloud.models.pocket import Pocket

    pid = await _pocket(source=REACT_SOURCE)
    before = (await Pocket.get(ObjectId(pid))).model_dump()
    cli["answers"]["visual-edit"] = {"ok": True, "content": APP.replace("<h1", "<h2", 1)}
    async with _client(_app()) as c:
        r = await c.post(URL.format(pid=pid), json={"src": "src/App.tsx:2:10", "kind": "delete"})
    assert r.status_code == 200, r.text
    after = (await Pocket.get(ObjectId(pid))).model_dump()
    # Same writer as patch_site_file: one saved change bumps the draft version.
    changed = {k for k in after if after[k] != before.get(k)}
    assert "source" in changed


@pytest.mark.parametrize(
    ("code", "status"),
    [("stale", 409), ("not_editable", 422), ("parse_error", 422), ("unsupported", 422)],
)
async def test_each_refusal_maps_to_its_status_and_saves_nothing(
    beanie_test_db, enqueued, cli, cli_text, code, status
):
    pid = await _pocket(source=REACT_SOURCE)
    cli["answers"]["visual-edit"] = {"ok": False, "code": code, "message": f"no: {code}"}
    async with _client(_app()) as c:
        r = await c.post(
            URL.format(pid=pid), json={"src": "src/App.tsx:2:10", "kind": "text", "value": "X"}
        )
    assert r.status_code == status, r.text
    assert f"visual_edit_{code}" in r.text
    assert f"no: {code}" in r.text
    assert (await _source(pid))["src/App.tsx"] == APP
    assert enqueued == []


async def test_a_moved_file_is_stale_before_the_generator_runs(
    beanie_test_db, enqueued, cli, cli_text
):
    pid = await _pocket(source=REACT_SOURCE)
    async with _client(_app()) as c:
        r = await c.post(
            URL.format(pid=pid),
            json={"src": "src/App.tsx:2:10", "kind": "text", "value": "X", "base_hash": "0" * 64},
        )
    assert r.status_code == 409, r.text
    assert "visual_edit_stale" in r.text
    assert cli["calls"] == []


async def test_a_generator_crash_is_500_and_saves_nothing(beanie_test_db, enqueued, cli, cli_text):
    pid = await _pocket(source=REACT_SOURCE)
    cli["code"] = 1
    async with _client(_app()) as c:
        r = await c.post(
            URL.format(pid=pid), json={"src": "src/App.tsx:2:10", "kind": "text", "value": "X"}
        )
    assert r.status_code == 500, r.text
    assert "visual_edit_failed" in r.text
    assert (await _source(pid))["src/App.tsx"] == APP
    assert enqueued == []


@pytest.mark.parametrize(
    ("src", "status"),
    [
        ("src/Missing.tsx:1:1", 404),
        ("../App.tsx:1:1", 422),
        ("package.json:1:1", 422),
    ],
)
async def test_src_must_name_an_editable_file_in_the_project(
    beanie_test_db, enqueued, cli, cli_text, src, status
):
    pid = await _pocket(source=REACT_SOURCE)
    async with _client(_app()) as c:
        r = await c.post(URL.format(pid=pid), json={"src": src, "kind": "delete"})
    assert r.status_code == status, r.text
    assert cli["calls"] == []


async def test_another_workspace_cannot_edit(beanie_test_db, enqueued, cli, cli_text):
    pid = await _pocket(source=REACT_SOURCE, workspace_id="ws2", owner_id="u2")
    async with _client(_app()) as c:
        r = await c.post(URL.format(pid=pid), json={"src": "src/App.tsx:2:10", "kind": "delete"})
    assert r.status_code == 404, r.text
    assert cli["calls"] == []


async def test_a_no_build_site_previews_at_once(beanie_test_db, cli, cli_text, monkeypatch):
    """A ``static`` edit rebuilds in-process: the answer is already ``passed``."""
    from pocketpaw_ee.sites import service as sites_service
    from pocketpaw_ee.sites import verify_store

    monkeypatch.setattr(sites_service, "_default_artifact_store", lambda: _Store())
    monkeypatch.setattr(verify_store, "default_verify_store", lambda: _Records())
    pid = await _pocket(source=NOBUILD)
    cli["answers"]["visual-edit"] = {
        "ok": True,
        "content": NOBUILD["index.html"].replace("Bakery", "Bread"),
    }
    cli["answers"]["stamp-html"] = {"files": {}}
    async with _client(_app()) as c:
        r = await c.post(
            URL.format(pid=pid), json={"src": "index.html:1:13", "kind": "text", "value": "Bread"}
        )
    assert r.status_code == 200, r.text
    assert r.json()["build_status"] == "passed"
    assert "Bread" in (await _source(pid))["index.html"]


# --------------------------------------------------------------- version guard


async def test_an_old_generator_answers_501(beanie_test_db, enqueued, cli, cli_text):
    cli_text["text"] = b"// project-build --site-config"
    pid = await _pocket(source=REACT_SOURCE)
    async with _client(_app()) as c:
        r = await c.post(URL.format(pid=pid), json={"src": "src/App.tsx:2:10", "kind": "delete"})
    assert r.status_code == 501, r.text
    assert "visual_edit_unavailable" in r.text
    assert cli["calls"] == []


def test_capabilities_follow_the_generator(cli_text):
    caps = engines.engine_capabilities("project")
    assert caps["select"] is caps["text"] is caps["structure"] is True
    assert caps["design"] is False and caps["sections"] is False
    assert caps["code"] is caps["build_log"] is True

    cli_text["text"] = b"// project-build --source-tags"
    caps = engines.engine_capabilities("project")
    assert caps["select"] is caps["text"] is caps["structure"] is False


def test_the_switch_turns_everything_off(cli_text, monkeypatch):
    monkeypatch.setenv(visual_edit.ENV_SWITCH, "0")
    assert not visual_edit.visual_edit_supported()
    assert not visual_edit.stamp_html_supported()
    assert not visual_edit.source_tags_supported()
    assert engines.engine_capabilities("project")["structure"] is False


def test_detection_reads_the_vendored_cli(monkeypatch, tmp_path):
    root = tmp_path / "gen"
    (root / "dist").mkdir(parents=True)
    (root / "package.json").write_text("{}")
    (root / "dist" / "cli.js").write_bytes(b'if (cmd === "arm-html") {}')
    monkeypatch.setenv(project_build.GEN_DIR_ENV, str(root))
    visual_edit.reset_cache()
    assert not visual_edit.visual_edit_supported()
    (root / "dist" / "cli.js").write_bytes(b'if (cmd === "visual-edit") {} // stamp-html')
    import os

    os.utime(root / "dist" / "cli.js", ns=(1, 10**18))
    assert visual_edit.visual_edit_supported()
    assert not visual_edit.stamp_html_supported()  # a bare word is not a command
    visual_edit.reset_cache()


# ----------------------------------------------- draft source tags (sandbox lane)


def test_the_hash_moves_with_the_tag_mode(cli_text):
    on = project_build.project_content_hash(SOURCE)
    cli_text["text"] = b"// old"
    off = project_build.project_content_hash(SOURCE)
    assert on != off
    assert project_build.draft_tag_mode(SOURCE) is None
    cli_text["text"] = FULL_CLI
    assert project_build.draft_tag_mode(SOURCE) == "source-tags"
    assert project_build.draft_tag_mode(NOBUILD) == "stamp-html"


def _gen(cli: bytes) -> list[tuple[bytes, str]]:
    return [(b"{}", "/tmp/paw-sites-gen/package.json"), (cli, "/tmp/paw-sites-gen/dist/cli.js")]


def _tagged_artifact() -> bytes:
    """A stage dir holding the untagged build plus the tagged one under _paw_tagged/."""
    plain = preview_origin.unpack_files(_bundle(STATIC_MANIFEST))
    files = dict(plain)
    for name, data in plain.items():
        if name.endswith("index.html"):
            data = b'<h1 data-paw-src="src/pages/index.astro:1:1">Hi</h1>'
        files[f"{project_build.TAGGED_STAGE}/{name}"] = data
    return _tar(files)


async def test_a_draft_builds_tagged_for_the_preview_and_untagged_for_publish(cli_text):
    store, records = _Store(), _Records()
    runner = _Runner(_result(artifact=_tagged_artifact()))
    out = await project_build.run_project_preview_build(
        {}, "pk1", "h1", {"source": SOURCE}, 600,
        _runner=runner, _store=store, _verify_store=records, _gen_uploads=_gen(FULL_CLI),
    )  # fmt: skip
    assert out["status"] == "built"
    uploads = {p: b for b, p in runner.calls[0]["extra_uploads"]}
    script = uploads[project_build.SANDBOX_BUILD_SCRIPT].decode()
    builds = [line for line in script.splitlines() if "cli.js project-build" in line]
    assert len(builds) == 2
    assert "--source-tags" not in builds[0]  # the bundle publish deploys
    assert "--source-tags" in builds[1]
    assert f"{project_build.TAGGED_STAGE}" in script

    # Publish reads bundle_key(hash): the untagged half, with no tagged copy inside.
    bundle = preview_origin.unpack_files(store.dist[("pk1", project_build.bundle_key("h1"))])
    assert not any(n.startswith(project_build.TAGGED_STAGE) for n in bundle)
    assert b"data-paw-src" not in bundle["dist/index.html"]
    # The preview is the tagged half.
    preview = preview_origin.unpack_files(store.dist[("pk1", "h1")])
    assert b"data-paw-src" in preview["index.html"]
    assert _record(records, "pk1", "h1")["source_tags"] is True


async def test_an_old_generator_never_gets_the_flag(cli_text):
    store = _Store()
    runner = _Runner(_result(artifact=_bundle(STATIC_MANIFEST)))
    await project_build.run_project_preview_build(
        {}, "pk1", "h2", {"source": SOURCE}, 600,
        _runner=runner, _store=store, _verify_store=_Records(),
        _gen_uploads=_gen(b"// project-build --site-config"),
    )  # fmt: skip
    uploads = {p: b for b, p in runner.calls[0]["extra_uploads"]}
    assert b"--source-tags" not in uploads[project_build.SANDBOX_BUILD_SCRIPT]
    assert store.dist[("pk1", project_build.bundle_key("h2"))] == _bundle(STATIC_MANIFEST)


def test_the_switch_keeps_the_flag_off_even_on_a_new_generator(cli_text, monkeypatch):
    monkeypatch.setenv(visual_edit.ENV_SWITCH, "off")
    assert project_build.draft_tag_mode(SOURCE) is None
    assert project_build.supports_source_tags(_gen(FULL_CLI))  # the file still has it


def test_split_keeps_an_untagged_artifact_byte_identical():
    artifact = _bundle(STATIC_MANIFEST)
    assert project_build.split_tagged_artifact(artifact) == (artifact, None)


# ------------------------------------------------ stamp-html (no-build lane)


async def test_static_preview_is_stamped_and_the_bundle_is_not(cli, cli_text):
    stamped = '<html><body><h1 data-paw-src="index.html:1:13">Bakery</h1></body></html>'
    cli["answers"]["stamp-html"] = {"files": {"index.html": stamped}}
    store, records = _Store(), _Records()
    out = await project_build.build_no_build_draft(
        "pk1", "h3", NOBUILD, store=store, records=records
    )
    assert out["status"] == "built"
    (call,) = cli["calls"]
    assert call["argv"][-1] == "stamp-html"
    sent = _payload(call)["files"]
    assert set(sent) == {"index.html", "about/index.html"}
    assert sent["index.html"] == NOBUILD["index.html"]  # the SOURCE text, line-true

    preview = preview_origin.unpack_files(store.dist[("pk1", "h3")])
    assert preview["index.html"].decode() == stamped
    assert b"data-paw-src" not in preview["about/index.html"]  # not answered: as it was
    bundle = preview_origin.unpack_files(store.dist[("pk1", project_build.bundle_key("h3"))])
    assert not any(b"data-paw-src" in data for data in bundle.values())
    assert _record(records, "pk1", "h3")["source_tags"] is True
    # The publish path re-stages from source and never stamps.
    _m, staged, _ = project_build.no_build_bundle(project_build.project_files(NOBUILD))
    assert not any(b"data-paw-src" in data for data in staged.values())


async def test_a_failed_stamp_serves_the_preview_plain(cli, cli_text):
    cli["code"] = 1
    store, records = _Store(), _Records()
    out = await project_build.build_no_build_draft(
        "pk1", "h4", NOBUILD, store=store, records=records
    )
    assert out["status"] == "built"
    preview = preview_origin.unpack_files(store.dist[("pk1", "h4")])
    assert preview["index.html"].decode() == NOBUILD["index.html"]
    assert _record(records, "pk1", "h4")["source_tags"] is False


async def test_no_stamp_without_the_command(cli, cli_text):
    cli_text["text"] = b"// project-build --source-tags"
    store = _Store()
    await project_build.build_no_build_draft("pk1", "h5", NOBUILD, store=store, records=_Records())
    assert cli["calls"] == []
