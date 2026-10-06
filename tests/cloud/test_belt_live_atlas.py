# tests/cloud/test_belt_live_atlas.py — a Belt run on the blueprint, live.
#
# Pins the backend half of the live Atlas (BF-5):
#   * the join: a repo-relative file maps to the C4 component whose ``paths``
#     glob is most specific (most literal chars, then fewest wildcards, then the
#     first declared); ``*`` stays inside one segment, ``**`` spans any number,
#     a trailing ``/`` means everything under it; only the model's own system
#     owns files; absolute or escaping paths map to nothing.
#   * the chai-ledger toy's blueprint (``fixtures/chai_ledger_c4.json``) maps its
#     real files.
#   * every Edit/Write/MultiEdit a seat makes publishes ``file_touched`` on the
#     run's stream with the component it maps to (none for a Read, a path
#     outside the repo or a cut input), its path scrubbed.
#   * ``GET /belt/runs/{id}/blueprint`` serves the bound repo's model as
#     committed on the run's base (never the working tree) with the run's
#     touched files mapped, and 404s a foreign run.

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

from fastapi.testclient import TestClient  # noqa: E402
from pocketpaw_ee.cloud.belt import feed as belt_feed  # noqa: E402
from pocketpaw_ee.cloud.belt import orient  # noqa: E402
from pocketpaw_ee.cloud.belt import service as belt_service  # noqa: E402
from pocketpaw_ee.cloud.belt.headless import HeadlessDevelopRunner  # noqa: E402

from tests.cloud.test_belt_console import _build_app, _propose_run  # noqa: E402
from tests.cloud.test_belt_develop_station import (  # noqa: E402
    _queue_run,
    _station,
    _write,
    repo,  # noqa: F401 — the tmp git repo fixture
)
from tests.cloud.test_belt_feed import (  # noqa: E402
    _SECRET,
    StreamingClaude,
    linked_tmp,  # noqa: F401 — station temp dirs behind a symlink
    store,  # noqa: F401 — the instinct store fixture
)
from tests.cloud.test_belt_live_feed import _entries, transport  # noqa: E402, F401

FIXTURES = Path(__file__).parent / "fixtures"
pytestmark = pytest.mark.usefixtures("any_repo_root")


def _model(*components: tuple[str, list[str]], scope: str = "app") -> dict:
    """A c4-gen model whose own system has one container holding
    ``components`` (id, paths), plus an external system that also claims files."""
    return {
        "scope": scope,
        "model": {
            "people": [],
            "systems": [
                {
                    "id": "elsewhere",
                    "containers": [{"id": "x", "components": [{"id": "theirs", "paths": ["**"]}]}],
                },
                {
                    "id": scope,
                    "containers": [
                        {
                            "id": "main",
                            "components": [
                                {"id": cid, "paths": paths} for cid, paths in components
                            ],
                        }
                    ],
                },
            ],
            "relationships": [],
        },
    }


def _owner(model: dict, path: str) -> str | None:
    return orient.component_for(path, orient.path_index(model))


# ---------------------------------------------------------------------------
# the join
# ---------------------------------------------------------------------------


def test_the_most_specific_glob_wins():
    model = _model(
        ("src", ["src/**"]),
        ("py", ["src/**/*.py"]),
        ("api", ["src/api/**"]),
        ("routes", ["src/api/routes.py"]),
        ("api-again", ["src/api/**"]),
    )
    assert _owner(model, "src/api/routes.py") == "routes"  # exact beats any glob
    # src/**/*.py (declared first) and src/api/** have as many literal chars;
    # fewer wildcards wins, and of the two src/api/** the first declared does.
    assert _owner(model, "src/api/v1/users.py") == "api"
    assert _owner(model, "src/core/x.py") == "py"
    assert _owner(model, "src/core/x.ts") == "src"
    assert _owner(model, "README.md") is None  # the external system owns nothing here


def test_glob_wildcards_respect_segments():
    model = _model(
        ("lib", ["lib/*"]),
        ("tests", ["**/test_*.py"]),
        ("docs", ["docs/"]),
        ("one", ["v?.txt"]),
    )
    assert _owner(model, "lib/a.py") == "lib"
    assert _owner(model, "lib/a/b.py") is None  # * never crosses a /
    assert _owner(model, "test_x.py") == "tests"  # **/ matches no directory too
    assert _owner(model, "a/b/test_x.py") == "tests"
    assert _owner(model, "docs/a/b.md") == "docs"  # a trailing / is everything under it
    assert _owner(model, "docsx/a.md") is None
    assert _owner(model, "v1.txt") == "one" and _owner(model, "v10.txt") is None


def test_repo_paths_are_normalised_and_escapes_refused():
    assert orient.repo_path("./src/a.py") == "src/a.py"
    assert orient.repo_path("src//b/../a.py") == "src/a.py"
    for bad in ("/etc/passwd", "../x.py", "a/../../x", "", ".", "   "):
        assert orient.repo_path(bad) is None, bad


def test_a_model_without_paths_or_unreadable_maps_nothing():
    assert orient.path_index(None) == []
    assert orient.path_index(_model(("a", []))) == []
    assert orient.load_model("not json") is None
    assert orient.load_model(json.dumps({"model": {}})) is None
    assert orient.load_model(json.dumps(_model(("a", ["x"]))))["scope"] == "app"


def test_the_chai_ledger_blueprint_maps_its_files(tmp_path: Path):
    model = orient.load_model((FIXTURES / "chai_ledger_c4.json").read_text())
    assert model is not None
    index = orient.path_index(model)
    owners = {
        p: orient.component_for(p, index)
        for p in (
            "chai_ledger/__main__.py",
            "chai_ledger/__init__.py",
            "tests/test_cli.py",
            "pyproject.toml",
            "uv.lock",
            "notes/todo.md",
        )
    }
    assert owners == {
        "chai_ledger/__main__.py": "ledger",
        "chai_ledger/__init__.py": "ledger",
        "tests/test_cli.py": "ledger-tests",
        "pyproject.toml": "project",
        "uv.lock": "project",
        "notes/todo.md": None,
    }
    # The architecture list the foreman reads is unchanged by paths.
    (tmp_path / "docs" / "c4").mkdir(parents=True)
    (tmp_path / "docs" / "c4" / "model.json").write_text(json.dumps(model))
    lines = orient.c4_lines(tmp_path)
    assert "- chai-ledger CLI / Ledger: The commands and the JSON-file store:" in lines[0]


# ---------------------------------------------------------------------------
# live: file_touched on the run's stream
# ---------------------------------------------------------------------------


def _commit_model(root: Path, model: dict) -> None:
    (root / "docs" / "c4").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "c4" / "model.json").write_text(json.dumps(model))
    for args in (["add", "-A"], ["commit", "-q", "-m", "blueprint"]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=root,
            check=True,
            capture_output=True,
        )


async def test_every_edit_publishes_file_touched_with_its_component(
    repo,  # noqa: F811
    store,  # noqa: F811
    mongo_db,
    monkeypatch,
    linked_tmp,  # noqa: F811
    transport,  # noqa: F811
):
    _commit_model(repo, _model(("feature", ["feature.txt"]), ("docs", ["docs/**"])))
    action_id = await _queue_run(monkeypatch, repo, recipe="")
    fake = StreamingClaude(develop=[_write("ok")])
    await HeadlessDevelopRunner(develop_fn=_station(fake, repo, checks=())).run(action_id)

    frames = _entries(transport, action_id)
    touched = [d for _, e, d in frames if e == "file_touched"]
    assert touched == [
        {
            "stage": "develop",
            "path": "feature.txt",
            "component": "feature",
            "tool": "Write",
            "call_id": "toolu_2",
        },
        {
            "stage": "develop",
            "path": "feature.txt",
            "component": "feature",
            "tool": "Edit",
            "call_id": "toolu_3",
        },
    ]
    # Each one right after the call it reports, never for a Read.
    for i, (_, event, data) in enumerate(frames):
        if event == "file_touched":
            assert frames[i - 1][1] == "tool_start"
            assert frames[i - 1][2]["call_id"] == data["call_id"]
    sent = json.dumps(touched)
    assert str(linked_tmp) not in sent and "belt-develop-" not in sent

    # The blueprint route maps the same file to the same component.
    blueprint = await belt_service.get_run_blueprint("w1", action_id)
    assert blueprint["files"] == [{"path": "feature.txt", "component": "feature"}]
    assert blueprint["ref"] == "main"
    assert blueprint["model"]["scope"] == "app"
    comps = blueprint["model"]["model"]["systems"][1]["containers"][0]["components"]
    assert comps[0] == {"id": "feature", "paths": ["feature.txt"]}


async def test_an_edit_with_no_blueprint_still_reports_the_file(
    repo,  # noqa: F811
    store,  # noqa: F811
    mongo_db,
    monkeypatch,
    transport,  # noqa: F811
):
    action_id = await _queue_run(monkeypatch, repo, recipe="")
    fake = StreamingClaude(develop=[_write("ok")])
    await HeadlessDevelopRunner(develop_fn=_station(fake, repo, checks=())).run(action_id)
    touched = [d for _, e, d in _entries(transport, action_id) if e == "file_touched"]
    assert [(d["path"], d["component"]) for d in touched] == [("feature.txt", None)] * 2
    blueprint = await belt_service.get_run_blueprint("w1", action_id)
    assert blueprint["model"] is None
    assert blueprint["files"] == [{"path": "feature.txt", "component": None}]


async def test_file_touched_is_scrubbed_and_only_names_repo_files(monkeypatch):
    sent: list[tuple[str, dict]] = []

    async def publish(_aid, event, data):
        sent.append((event, data))

    async def nothing(*_a, **_k):
        return None

    monkeypatch.setattr(belt_feed, "_publish", publish)
    monkeypatch.setattr(belt_feed, "_notify", nothing)
    feed = belt_feed.RunFeed("w1", "run-x", nothing)
    feed.paths = orient.path_index(_model(("keys", ["keys/"])))
    await feed.stage("fix")
    for tool, tool_input in (
        ("Write", {"file_path": f"keys/{_SECRET}.txt"}),
        ("Read", {"file_path": "keys/a.txt"}),
        ("Edit", {"file_path": "/Users/user/elsewhere.py"}),
        ("Edit", '{"file_path": "cut…'),
        ("MultiEdit", {"file_path": "keys/b.txt", "edits": []}),
    ):
        await feed.add("tool_start", {"tool": tool, "input": tool_input, "call_id": tool})
    touched = [d for e, d in sent if e == "file_touched"]
    assert [(d["path"], d["component"], d["stage"]) for d in touched] == [
        ("keys/[REDACTED].txt", "keys", "fix"),
        ("keys/b.txt", "keys", "fix"),
    ]
    assert _SECRET not in json.dumps(sent)


# ---------------------------------------------------------------------------
# GET /belt/runs/{id}/blueprint
# ---------------------------------------------------------------------------


async def test_the_blueprint_is_read_at_the_base_not_the_working_tree(
    repo,  # noqa: F811
    store,  # noqa: F811
    mongo_db,
):
    _commit_model(repo, _model(("app", ["app.py"]), ("lib", ["lib/**"])))
    # An uncommitted edit in the owner's checkout is not the run's base.
    (repo / "docs" / "c4" / "model.json").write_text(json.dumps(_model(("other", ["**"]))))
    diff = (
        "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/lib/logo.png b/lib/logo.png\nnew file mode 100644\n"
        "GIT binary patch\nliteral 3\nKcmZ?l004N+0RR91\n\n"
        "diff --git a/notes.md b/notes.md\n--- /dev/null\n+++ b/notes.md\n@@ -0,0 +1 @@\n+n\n"
    )
    run = await _propose_run(store, repo=str(repo), diff=diff)
    # The seats' stored edits come first (develop, then fix), then the diff's
    # files; a read, a path outside the repo and a cut input name nothing.
    fix = [
        {"id": "s1", "kind": "tool", "tool": "Read", "input": {"file_path": "secret.md"}},
        {"id": "s2", "kind": "tool", "tool": "Edit", "input": {"file_path": "lib/util.py"}},
        {"id": "s3", "kind": "tool", "tool": "Write", "input": {"file_path": "/Users/user/x.py"}},
        {"id": "s4", "kind": "tool", "tool": "Edit", "input": '{"file_path": "cut…'},
    ]
    await belt_service.save_run_feed("w1", run.id, "fix", fix)
    develop = [
        {"id": "s0", "kind": "tool", "tool": "MultiEdit", "input": {"file_path": "./app.py"}}
    ]
    await belt_service.save_run_feed("w1", run.id, "develop", develop)
    blueprint = await belt_service.get_run_blueprint("w1", run.id)
    assert blueprint["files"] == [
        {"path": "app.py", "component": "app"},
        {"path": "lib/util.py", "component": "lib"},
        {"path": "lib/logo.png", "component": "lib"},
        {"path": "notes.md", "component": None},
    ]
    assert [
        c["id"] for c in blueprint["model"]["model"]["systems"][1]["containers"][0]["components"]
    ] == [
        "app",
        "lib",
    ]
    assert "source_path" not in blueprint["model"]

    client = TestClient(_build_app(role="member"))
    res = client.get(f"/api/v1/belt/runs/{run.id}/blueprint")
    assert res.status_code == 200 and res.json()["files"] == blueprint["files"]
    foreign = TestClient(_build_app(workspace_id="w2"))
    res = foreign.get(f"/api/v1/belt/runs/{run.id}/blueprint")
    assert res.status_code == 404 and res.json()["error"]["code"] == "belt.run_not_found"


async def test_a_repo_outside_the_allowlist_serves_no_model(
    repo,  # noqa: F811
    store,  # noqa: F811
    mongo_db,
    monkeypatch,
):
    _commit_model(repo, _model(("app", ["app.py"])))
    run = await _propose_run(store, repo=str(repo))
    from tests.cloud.test_belt_develop_station import _allowlist

    _allowlist(monkeypatch, ["/nowhere"])
    blueprint = await belt_service.get_run_blueprint("w1", run.id)
    assert blueprint["model"] is None and blueprint["ref"] is None
    assert blueprint["files"] == [{"path": "app.py", "component": None}]


async def test_a_hostile_base_branch_is_never_passed_to_git(
    repo,  # noqa: F811
    store,  # noqa: F811
    mongo_db,
    monkeypatch,
):
    from pocketpaw_ee.cloud.belt import executor

    _commit_model(repo, _model(("app", ["app.py"])))
    calls: list[list[str]] = []
    real_run = executor._run

    async def spy(argv, **kw):
        calls.append(list(argv))
        return await real_run(argv, **kw)

    monkeypatch.setattr(executor, "_run", spy)
    for base in ("--output=/tmp/x", "main..evil", "-c"):
        run = await _propose_run(store, repo=str(repo), base_branch=base)
        blueprint = await belt_service.get_run_blueprint("w1", run.id)
        assert blueprint["model"] is None and blueprint["ref"] is None
    assert calls == []
    run = await _propose_run(store, repo=str(repo))
    assert (await belt_service.get_run_blueprint("w1", run.id))["ref"] == "main"
    assert calls  # the spy sees a real read
