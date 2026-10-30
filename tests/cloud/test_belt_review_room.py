# tests/cloud/test_belt_review_room.py — the Review Room backend
# (``ee/pocketpaw_ee/cloud/belt/review.py`` behind ``belt/router.py``), pinned to
# paw-enterprise docs/design/belt-review-room.md:
#   * try: macOS only (501 elsewhere); the CI ``app-builds`` artifact of the
#     run's merged PR is downloaded, its tarball unpacked (traversal refused),
#     the line's app picked (ambiguous = the list, nothing opens), quarantine
#     stripped and ``open``ed; no artifact = 409; a live try is joined (200).
#   * reviews: multipart limits (frames, video, frame bytes, whole body) are 413,
#     wrong media 415, bad notes 400; a needs_changes review files an issue with
#     the line's labels and frame links; an Idempotency-Key replays; media are
#     served only for listed names, never across workspaces or out of the dir.
# gh, tar-free paths and ``open`` run through a fake ``Runner``; ``tar`` is real.

from __future__ import annotations

import io
import json
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("pocketpaw_ee")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from pocketpaw.instinct.store import InstinctStore  # noqa: E402
from tests.cloud.test_belt_console import _propose_run  # noqa: E402

SLUG = "qbtrix/creative-apps"
HEAD = "a" * 40
MERGE = "b" * 40
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
APPS = ["FlareApp", "DesignApp", "PhotoApp"]


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> InstinctStore:
    st = InstinctStore(tmp_path / "instinct_review.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: st)
    return st


@pytest.fixture(autouse=True)
def dirs(tmp_path: Path, monkeypatch):
    from pocketpaw_ee.cloud.belt import review

    monkeypatch.setenv("POCKETPAW_FACTORY_APP_DIR", str(tmp_path / "apps"))
    monkeypatch.setenv("POCKETPAW_FACTORY_REVIEW_DIR", str(tmp_path / "reviews"))
    monkeypatch.setenv("POCKETPAW_FRONTEND_BASE_URL", "https://paw.example")
    review._TRIES.clear()
    return tmp_path


def _tarball(apps: list[str], extra: tarfile.TarInfo | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for app in apps:
            data = b"#!/bin/sh\n"
            info = tarfile.TarInfo(f"{app}.app/Contents/MacOS/{app}")
            info.size, info.mode = len(data), 0o755
            tar.addfile(info, io.BytesIO(data))
        if extra is not None:
            tar.addfile(extra, io.BytesIO(b"x" * extra.size))
    return buf.getvalue()


class FakeRunner:
    """gh, xattr and open faked; ``tar`` really runs."""

    def __init__(self, *, built: set[str] | None = None, tgz: bytes = b"", issue_code: int = 0):
        self.calls: list[list[str]] = []
        self.stdins: dict[str, str] = {}
        self.built = {MERGE} if built is None else built
        self.tgz = tgz or _tarball(APPS)
        self.issue_code = issue_code

    async def __call__(self, argv, *, cwd, timeout, stdin=None):
        from pocketpaw_ee.cloud.belt.develop_station import run_subprocess

        argv = list(argv)
        self.calls.append(argv)
        if stdin is not None:
            self.stdins[" ".join(argv[:4])] = stdin
        if argv[0] == "tar":
            return await run_subprocess(argv, cwd=cwd, timeout=timeout)
        if argv[:3] == ["gh", "pr", "view"]:
            pr = {"state": "MERGED", "headRefOid": HEAD, "mergeCommit": {"oid": MERGE}}
            return 0, json.dumps(pr), ""
        if argv[:2] == ["gh", "api"] and "/actions/runs?head_sha=" in argv[2]:
            sha = argv[2].rsplit("=", 1)[1]
            runs = [{"id": 901}] if sha in self.built else []
            return 0, json.dumps({"workflow_runs": runs}), ""
        if argv[:2] == ["gh", "api"] and argv[2].endswith("/artifacts"):
            return 0, json.dumps({"artifacts": [{"name": "app-builds", "expired": False}]}), ""
        if argv[:3] == ["gh", "run", "download"]:
            out = Path(argv[argv.index("-D") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / "app-builds.tar.gz").write_bytes(self.tgz)
            return 0, "", ""
        if argv[:3] == ["gh", "issue", "create"]:
            if self.issue_code:
                return 1, "", "could not add label: 'gap' not found"
            return 0, f"https://github.com/{SLUG}/issues/42\n", ""
        if argv[:2] == ["gh", "api"] and "/git/ref/heads/" in argv[2]:
            return 1, "", "HTTP 404"
        if argv[:2] == ["gh", "api"] and argv[2].endswith("/commits/HEAD"):
            return 0, "c" * 40 + "\n", ""
        return 0, "", ""  # gh label create, git refs POST, contents PUT, xattr, open

    def ran(self, program: str) -> list[list[str]]:
        return [c for c in self.calls if c[0] == program]


def _app(runner: FakeRunner, *, role="member", workspace_id="w1", labels=("line:flare",)):
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.belt.review import ReviewRoom
    from pocketpaw_ee.cloud.belt.router import review_room_dep, router
    from pocketpaw_ee.cloud.license import require_license

    async def charter(workspace_id: str, mandate_id: str) -> dict:
        return {"repo": "x", "charter": {"issue_labels": list(labels)}}

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[require_license] = lambda: None
    user = SimpleNamespace(
        id="u1",
        email="ada@example.com",
        full_name="Ada",
        active_workspace=workspace_id,
        workspaces=[SimpleNamespace(workspace=workspace_id, role=role)],
    )

    async def fake_user():
        return user

    app.dependency_overrides[current_active_user] = fake_user
    room = ReviewRoom(runner=runner, background=False, charter_for=charter)
    app.dependency_overrides[review_room_dep] = lambda: room
    return app


async def _landed_run(store, workspace_id: str = "w1", title: str = "Glow on text") -> str:
    action = await _propose_run(store, workspace_id=workspace_id, task="#93 glow")
    params = dict(action.parameters)
    params["_code_change"] = {
        **params["_code_change"],
        "title": title,
        "mandate_id": "m1",
        "pr_url": f"https://github.com/{SLUG}/pull/9",
        "commit_sha": MERGE,
    }
    await store.update_parameters(action.id, params)
    return action.id


# ---------------------------------------------------------------------------
# try
# ---------------------------------------------------------------------------


async def test_try_opens_the_lines_app_on_a_mac(store, dirs, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    run = await _landed_run(store)
    runner = FakeRunner()
    with TestClient(_app(runner)) as client:
        res = client.post(f"/api/v1/belt/runs/{run}/try")
        state = client.get(f"/api/v1/belt/runs/{run}/try").json()
        again = client.post(f"/api/v1/belt/runs/{run}/try")

    assert res.status_code == 202, res.text
    assert state["state"] == "opened", state
    assert state["artifact"] == "app-builds" and state["started_at"].endswith("Z")
    bundle = dirs / "apps" / run / "apps" / "FlareApp.app"
    assert (bundle / "Contents" / "MacOS" / "FlareApp").is_file()
    assert runner.ran("xattr") == [["xattr", "-dr", "com.apple.quarantine", str(bundle)]]
    assert runner.ran("open") == [["open", str(bundle)]]
    # The merge commit's CI was asked first; a live try is joined, not restarted.
    heads = [c[2] for c in runner.calls if c[:2] == ["gh", "api"] and "head_sha" in c[2]]
    assert heads[0].endswith(MERGE)
    assert again.status_code == 200 and len(runner.ran("open")) == 1


async def test_try_falls_back_to_the_pr_head_build(store, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    run = await _landed_run(store)
    runner = FakeRunner(built={HEAD})
    with TestClient(_app(runner)) as client:
        assert client.post(f"/api/v1/belt/runs/{run}/try").status_code == 202
    assert runner.ran("open")


async def test_try_is_501_off_a_mac(store, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    run = await _landed_run(store)
    runner = FakeRunner()
    with TestClient(_app(runner)) as client:
        res = client.post(f"/api/v1/belt/runs/{run}/try")
        idle = client.get(f"/api/v1/belt/runs/{run}/try").json()
    assert res.status_code == 501
    assert res.json()["error"]["code"] == "belt.try_unsupported"
    assert res.json()["detail"]
    assert idle["state"] == "idle"
    assert runner.calls == []


async def test_try_without_an_artifact_is_409(store, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    run = await _landed_run(store)
    with TestClient(_app(FakeRunner(built=set()))) as client:
        res = client.post(f"/api/v1/belt/runs/{run}/try")
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "belt.no_artifact"


async def test_an_ambiguous_app_opens_nothing_and_lists_them(store, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    run = await _landed_run(store, title="Timeline strip spacing")
    runner = FakeRunner()
    with TestClient(_app(runner, labels=("line:suiteui",))) as client:
        client.post(f"/api/v1/belt/runs/{run}/try")
        state = client.get(f"/api/v1/belt/runs/{run}/try").json()
    assert state["state"] == "failed"
    assert state["apps"] == ["DesignApp.app", "FlareApp.app", "PhotoApp.app"]
    assert "FlareApp.app" in state["reason"]
    assert runner.ran("open") == []


async def test_the_title_picks_the_app_when_the_line_names_none(store, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    run = await _landed_run(store, title="Design: snap guides")
    runner = FakeRunner()
    with TestClient(_app(runner, labels=())) as client:
        client.post(f"/api/v1/belt/runs/{run}/try")
    assert runner.ran("open")[0][1].endswith("DesignApp.app")


@pytest.mark.parametrize(
    "name, linkname",
    [("../escape.txt", ""), ("/abs.txt", ""), ("FlareApp.app/link", "../../../outside")],
)
async def test_a_tarball_with_traversal_is_refused(store, dirs, monkeypatch, name, linkname):
    monkeypatch.setattr(sys, "platform", "darwin")
    run = await _landed_run(store)
    extra = tarfile.TarInfo(name)
    if linkname:
        extra.type, extra.linkname = tarfile.SYMTYPE, linkname
    else:
        extra.size = 1
    runner = FakeRunner(tgz=_tarball(["FlareApp"], extra))
    with TestClient(_app(runner)) as client:
        client.post(f"/api/v1/belt/runs/{run}/try")
        state = client.get(f"/api/v1/belt/runs/{run}/try").json()
    assert state["state"] == "failed" and "unsafe" in state["reason"]
    assert runner.ran("tar") == [] and runner.ran("open") == []
    assert not (dirs / "apps" / run / "escape.txt").exists()


async def test_a_try_preparing_too_long_reads_failed(store, monkeypatch):
    from pocketpaw_ee.cloud.belt import review

    run = await _landed_run(store)
    review._TRIES[run] = {"state": "preparing", "_t": 0.0, "updated_at": None}
    with TestClient(_app(FakeRunner())) as client:
        state = client.get(f"/api/v1/belt/runs/{run}/try").json()
    assert state["state"] == "failed"
    assert state["reason"] == "Timed out preparing the app."


# ---------------------------------------------------------------------------
# reviews
# ---------------------------------------------------------------------------


NOTES = [{"t_ms": 12500, "text": " Save button does nothing "}, {"t_ms": 3000, "text": "Slow"}]


def _post(client, run, *, verdict="needs_changes", notes=NOTES, video=WEBM, frames=None, **kw):
    # Text parts ride as (None, value) so the body is multipart even with no files.
    files = [("verdict", (None, verdict)), ("notes", (None, json.dumps(notes)))]
    if video is not None:
        files.append(("video", ("review.webm", video, "video/webm")))
    for name, data, kind in frames if frames is not None else [("12500.png", PNG, "image/png")]:
        files.append(("frames", (name, data, kind)))
    return client.post(f"/api/v1/belt/runs/{run}/reviews", files=files, **kw)


async def test_needs_changes_is_stored_and_files_an_issue(store, dirs):
    run = await _landed_run(store)
    runner = FakeRunner()
    with TestClient(_app(runner, labels=("line:flare",))) as client:
        res = _post(client, run, headers={"Idempotency-Key": "k-1"})
        listed = client.get(f"/api/v1/belt/runs/{run}/reviews").json()
        body = res.json()
        video = client.get(f"/api/v1{body['video_url']}")
        part = client.get(f"/api/v1{body['video_url']}", headers={"Range": "bytes=0-3"})
        frame = client.get(f"/api/v1{body['frames'][0]}")
        detail = client.get(f"/api/v1/belt/runs/{run}").json()

    assert res.status_code == 201, res.text
    rid = body["id"]
    assert body["run_id"] == run and body["by"] == "user:ada@example.com"
    assert body["notes"] == [
        {"t_ms": 3000, "text": "Slow"},
        {"t_ms": 12500, "text": "Save button does nothing"},
    ]
    assert body["video_url"] == f"/belt/runs/{run}/reviews/{rid}/video"
    assert body["frames"] == [f"/belt/runs/{run}/reviews/{rid}/frames/12500.png"]
    assert body["issue_url"] == f"https://github.com/{SLUG}/issues/42"
    assert body["issue_error"] is None
    assert listed == [body]
    assert video.status_code == 200 and video.content == WEBM
    assert video.headers["content-type"] == "video/webm"
    assert "immutable" in video.headers["cache-control"]
    assert part.status_code == 206 and part.content == WEBM[:4]
    assert frame.status_code == 200 and frame.content == PNG
    assert (dirs / "reviews" / run / rid / "12500.png").read_bytes() == PNG
    assert detail["issue_url"] is None  # the run's own issue, not the review's

    (create,) = [c for c in runner.calls if c[:3] == ["gh", "issue", "create"]]
    labels = [create[i + 1] for i, a in enumerate(create) if a == "--label"]
    assert labels == ["review", "gap", "bug", "line:flare"]
    assert create[create.index("--title") + 1] == "Review: Glow on text needs changes"
    issue = runner.stdins["gh issue create -R"]
    assert "**00:12** Save button does nothing" in issue and "**00:03** Slow" in issue
    assert f"https://paw.example/belt-next/runs/{run}/review" in issue
    assert f"https://github.com/{SLUG}/raw/belt-review-assets/reviews/{rid}/12500.png" in issue
    assert "ada@example.com" not in issue and "Ada" in issue
    put = json.loads(runner.stdins["gh api -X PUT"])
    assert put["branch"] == "belt-review-assets"


async def test_looks_good_files_no_issue(store):
    run = await _landed_run(store)
    runner = FakeRunner()
    with TestClient(_app(runner)) as client:
        res = _post(client, run, verdict="looks_good", notes=[], video=None, frames=[])
    assert res.status_code == 201, res.text
    assert res.json()["issue_url"] is None and res.json()["video_url"] is None
    assert runner.calls == []


async def test_an_idempotency_key_replays_the_stored_review(store):
    run = await _landed_run(store)
    runner = FakeRunner()
    with TestClient(_app(runner)) as client:
        first = _post(client, run, headers={"Idempotency-Key": "same"})
        again = _post(client, run, headers={"Idempotency-Key": "same"})
        listed = client.get(f"/api/v1/belt/runs/{run}/reviews").json()
    assert first.status_code == 201 and again.status_code == 200
    assert again.json() == first.json()
    assert len(listed) == 1
    assert len([c for c in runner.calls if c[:3] == ["gh", "issue", "create"]]) == 1


async def test_a_refused_issue_keeps_the_review(store):
    run = await _landed_run(store)
    with TestClient(_app(FakeRunner(issue_code=1))) as client:
        res = _post(client, run)
        listed = client.get(f"/api/v1/belt/runs/{run}/reviews").json()
    assert res.status_code == 201
    assert res.json()["issue_url"] is None
    assert "GitHub refused the issue" in res.json()["issue_error"]
    assert listed[0]["issue_error"] == res.json()["issue_error"]


@pytest.mark.parametrize(
    "limit, value, kwargs",
    [
        (
            "REVIEW_FRAMES",
            1,
            {"frames": [("12500.png", PNG, "image/png"), ("3000.png", PNG, "image/png")]},
        ),
        ("REVIEW_VIDEO_BYTES", 8, {}),
        ("REVIEW_FRAME_BYTES", 8, {}),
        ("REVIEW_REQUEST_BYTES", 64, {}),
    ],
)
async def test_review_limits_are_413(store, monkeypatch, limit, value, kwargs):
    from pocketpaw_ee.cloud.belt import review

    monkeypatch.setattr(review, limit, value)
    run = await _landed_run(store)
    with TestClient(_app(FakeRunner())) as client:
        res = _post(client, run, **kwargs)
        listed = client.get(f"/api/v1/belt/runs/{run}/reviews").json()
    assert res.status_code == 413, res.text
    assert res.json()["error"]["code"] == "belt.review_too_large"
    assert listed == []


async def test_review_limits_hold_at_the_contract_numbers():
    from pocketpaw_ee.cloud.belt import review

    assert review.REVIEW_VIDEO_BYTES == 209_715_200
    assert review.REVIEW_FRAMES == 60
    assert review.REVIEW_FRAME_BYTES == 5 * 1024 * 1024
    assert review.REVIEW_REQUEST_BYTES == 512 * 1024 * 1024


@pytest.mark.parametrize(
    "kwargs, code",
    [
        ({"video": b"not webm at all"}, 415),
        ({"frames": [("12500.png", b"GIF89a....", "image/png")]}, 415),
        ({"frames": [("12500.png", PNG, "image/gif")]}, 415),
        ({"frames": [("999.png", PNG, "image/png")]}, 400),
        ({"frames": [("../12500.png", PNG, "image/png")]}, 400),
        ({"verdict": "meh"}, 400),
        ({"notes": []}, 400),
        ({"notes": [{"t_ms": -1, "text": "x"}]}, 400),
        ({"notes": [{"t_ms": 1, "text": "   "}]}, 400),
        ({"notes": [{"t_ms": 1, "text": "x" * 2001}]}, 400),
    ],
)
async def test_bad_reviews_are_refused(store, kwargs, code):
    run = await _landed_run(store)
    with TestClient(_app(FakeRunner())) as client:
        res = _post(client, run, **kwargs)
    assert res.status_code == code, res.text
    want = "belt.review_bad_media" if code == 415 else "belt.review_invalid"
    assert res.json()["error"]["code"] == want


async def test_review_media_refuse_unknown_names_and_traversal(store, dirs):
    run = await _landed_run(store)
    with TestClient(_app(FakeRunner())) as client:
        body = _post(client, run).json()
        rid = body["id"]
        folder = dirs / "reviews" / run / rid
        (folder / "777.png").write_bytes(PNG)  # on disk, not on the blob
        (dirs / "secret.png").write_bytes(PNG)
        (folder / "12500.png").unlink()
        (folder / "12500.png").symlink_to(dirs / "secret.png")  # listed, leads out
        base = f"/api/v1/belt/runs/{run}/reviews"
        codes = [
            client.get(f"{base}/{rid}/frames/777.png").status_code,
            client.get(f"{base}/{rid}/frames/12500.png").status_code,
            client.get(f"{base}/{rid}/frames/..%2F..%2Fsecret.png").status_code,
            client.get(f"{base}/rv_0000000000000000/video").status_code,
            client.get(f"{base}/..%2F{rid}/video").status_code,
        ]
    assert codes == [404] * 5


async def test_review_routes_are_workspace_scoped(store, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    run = await _landed_run(store, workspace_id="w-other")
    runner = FakeRunner()
    with TestClient(_app(runner, workspace_id="w1")) as client:
        codes = [
            client.post(f"/api/v1/belt/runs/{run}/try").status_code,
            client.get(f"/api/v1/belt/runs/{run}/try").status_code,
            _post(client, run).status_code,
            client.get(f"/api/v1/belt/runs/{run}/reviews").status_code,
            client.get(f"/api/v1/belt/runs/{run}/reviews/rv_0000000000000000/video").status_code,
        ]
    assert codes == [404] * 5
    assert runner.calls == []


async def test_review_routes_need_a_workspace_member(store):
    """``belt.read`` is MEMBER, the lowest role: a caller outside the workspace
    is refused before anything runs."""
    run = await _landed_run(store)
    runner = FakeRunner()
    app = _app(runner)
    from pocketpaw_ee.cloud.auth import current_active_user

    outsider = SimpleNamespace(id="u9", email="x@example.com", active_workspace="w1", workspaces=[])

    async def fake_outsider():
        return outsider

    app.dependency_overrides[current_active_user] = fake_outsider
    with TestClient(app) as client:
        codes = [
            client.post(f"/api/v1/belt/runs/{run}/try").status_code,
            _post(client, run).status_code,
            client.get(f"/api/v1/belt/runs/{run}/reviews").status_code,
        ]
    assert codes == [403] * 3
    assert runner.calls == []


def _raw(run, monkeypatch=None):
    """The multipart body ``_post`` would send, and its content type."""
    import httpx

    files = [("verdict", (None, "needs_changes")), ("notes", (None, json.dumps(NOTES)))]
    files.append(("video", ("review.webm", WEBM * 8, "video/webm")))
    req = httpx.Request("POST", "http://t/", files=files)
    return req.read(), req.headers["content-type"]


async def test_a_chunked_body_is_cut_off_mid_stream(store, monkeypatch):
    """No Content-Length: the byte counter on the stream refuses it."""
    from pocketpaw_ee.cloud.belt import review

    monkeypatch.setattr(review, "REVIEW_REQUEST_BYTES", 300)
    run = await _landed_run(store)
    body, kind = _raw(run)
    assert len(body) > 300

    def chunks():
        for i in range(0, len(body), 64):
            yield body[i : i + 64]

    with TestClient(_app(FakeRunner())) as client:
        res = client.post(
            f"/api/v1/belt/runs/{run}/reviews", content=chunks(), headers={"content-type": kind}
        )
    assert res.status_code == 413, res.text
    assert "in all" in res.json()["detail"]


async def test_a_declared_oversize_body_is_refused_before_reading(store, monkeypatch):
    from pocketpaw_ee.cloud.belt import review

    run = await _landed_run(store)
    body, kind = _raw(run)
    monkeypatch.setattr(review, "REVIEW_REQUEST_BYTES", len(body) + 10)
    with TestClient(_app(FakeRunner())) as client:
        res = client.post(
            f"/api/v1/belt/runs/{run}/reviews",
            content=body,
            headers={"content-type": kind, "content-length": str(len(body) + 11)},
        )
    assert res.status_code == 413, res.text
