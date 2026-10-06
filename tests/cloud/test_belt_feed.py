# tests/cloud/test_belt_feed.py — the develop station's step feed.
#
# A queued run goes through the REAL headless runner and develop station (tmp
# git repo, real check command); only the ``claude`` binary is faked, and its
# develop seat prints the stream-json a real ``claude -p --output-format
# stream-json --verbose`` run prints (shapes captured from live CLI runs:
# thinking with an empty body, text, tool_use, tool_result carrying only the
# tool_use_id, the result envelope, then one more system line). Pins:
#   * the seat streams and the station still reads the result envelope;
#   * steps stored per run + stage in order (mongomock), served by
#     ``GET /belt/runs/{id}/feed`` in the chat wire shape, prose blocks a blank
#     line apart;
#   * no worktree path anywhere: the temp dir is reached through a symlink and
#     the CLI reports the physical path (macOS ``/var`` -> ``/private/var``);
#     bare paths (``cd <wt>``, a ``pwd`` result) read ``.``; the bound repo the
#     ``.git`` file names goes too; same for headless_error (claude's words or
#     stderr, never stream-json) and a failing check's tail;
#   * secrets in a tool result or input reach neither storage nor the response;
#   * the stage row is the latest attempt: a failed, timed-out, or pre-seat
#     failing re-develop replaces it; the upsert keeps one row per
#     workspace/run/stage and its createdAt;
#   * parallel same-name calls pair with their results by id; a failing save
#     never fails the run; the step and byte caps; the route's tenancy 404.

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

pytestmark = pytest.mark.usefixtures("any_repo_root")

from fastapi.testclient import TestClient  # noqa: E402
from pocketpaw_ee.cloud.belt import develop_station as ds  # noqa: E402
from pocketpaw_ee.cloud.belt.feed import FEED_MAX_STEPS, fold_feed, stream_events  # noqa: E402
from pocketpaw_ee.cloud.belt.headless import HeadlessDevelopRunner  # noqa: E402
from pocketpaw_ee.cloud.mandates import foreman  # noqa: E402

from pocketpaw.agents.protocol import AgentEvent  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402
from tests.cloud.test_belt_console import _build_app  # noqa: E402
from tests.cloud.test_belt_develop_station import (  # noqa: E402
    FAKE_CLAUDE,
    FakeClaude,
    _queue_run,
    _request,
    _station,
    _write,
    repo,  # noqa: F401 — the tmp git repo fixture
)

_SECRET = "sk-" + "a1B2c3D4e5F6g7H8i9J0" * 3
_INPUT_SECRET = "sk-" + "Z9y8X7w6V5u4T3s2R1q0" * 3


def _t(second: int) -> str:
    return f"2026-10-06T08:00:{second:02d}.000Z"


def _stream(cwd: Path, *, is_error: bool = False, cut: bool = False, git_text: str = "") -> str:
    """What the develop seat prints. ``cut`` = the CLI died mid-tool.
    ``git_text`` is what reading the worktree's ``.git`` file returns (it names
    the bound repo); the Bash calls carry the worktree path bare."""
    lines: list[dict] = [
        {"type": "system", "subtype": "init", "cwd": str(cwd), "session_id": "s1"},
        {
            "type": "assistant",
            "timestamp": _t(0),
            "message": {"content": [{"type": "thinking", "thinking": "", "signature": "x"}]},
        },
        {
            "type": "assistant",
            "timestamp": _t(0),
            "message": {"content": [{"type": "thinking", "thinking": "The task wants a file."}]},
        },
        {
            "type": "assistant",
            "timestamp": _t(1),
            "message": {"content": [{"type": "text", "text": f"I'll add {cwd}/feature.txt."}]},
        },
        {
            "type": "assistant",
            "timestamp": _t(2),
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Read",
                        "input": {"file_path": f"{cwd}/README.md"},
                    }
                ]
            },
        },
        {
            "type": "user",
            "timestamp": _t(3),
            "message": {
                "content": [
                    {
                        "tool_use_id": "toolu_1",
                        "type": "tool_result",
                        "content": f"1\ttoy\n2\tOPENAI_KEY={_SECRET}\n",
                    }
                ]
            },
        },
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
        {
            "type": "assistant",
            "timestamp": _t(4),
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_2",
                        "name": "Write",
                        "input": {"file_path": f"{cwd}/feature.txt", "content": "draft\n"},
                    }
                ]
            },
        },
    ]
    if cut:
        return "\n".join(json.dumps(x) for x in lines) + "\n"
    lines += [
        {
            "type": "user",
            "timestamp": _t(5),
            "message": {
                "content": [
                    {
                        "tool_use_id": "toolu_2",
                        "type": "tool_result",
                        "content": [{"type": "text", "text": f"File created at {cwd}/feature.txt"}],
                    }
                ]
            },
        },
        {
            "type": "assistant",
            "timestamp": _t(6),
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_3",
                        "name": "Edit",
                        "input": {
                            "file_path": f"{cwd}/feature.txt",
                            "old_string": "draft",
                            "new_string": f"ok # {_INPUT_SECRET}",
                        },
                    }
                ]
            },
        },
        {
            "type": "user",
            "timestamp": _t(7),
            "message": {
                "content": [
                    {"tool_use_id": "toolu_3", "type": "tool_result", "content": "Updated."}
                ]
            },
        },
        {
            "type": "assistant",
            "timestamp": _t(8),
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_4",
                        "name": "Read",
                        "input": {"file_path": f"{cwd}/.git"},
                    }
                ]
            },
        },
        {
            "type": "user",
            "timestamp": _t(8),
            "message": {
                "content": [{"tool_use_id": "toolu_4", "type": "tool_result", "content": git_text}]
            },
        },
        {
            "type": "assistant",
            "timestamp": _t(8),
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_5",
                        "name": "Bash",
                        "input": {"command": f"cd {cwd} && uv run pytest -q"},
                    }
                ]
            },
        },
        {
            "type": "user",
            "timestamp": _t(8),
            "message": {
                "content": [
                    {
                        "tool_use_id": "toolu_5",
                        "type": "tool_result",
                        "content": (
                            f"Permission to use Bash with command cd {cwd} && "
                            "uv run pytest -q has been denied."
                        ),
                    }
                ]
            },
        },
        {
            "type": "assistant",
            "timestamp": _t(8),
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_6",
                        "name": "Bash",
                        "input": {"command": "pwd"},
                    }
                ]
            },
        },
        {
            "type": "user",
            "timestamp": _t(8),
            "message": {
                "content": [{"tool_use_id": "toolu_6", "type": "tool_result", "content": str(cwd)}]
            },
        },
        {
            "type": "assistant",
            "timestamp": _t(8),
            "message": {"content": [{"type": "text", "text": "Done: feature.txt says ok."}]},
        },
        {
            "type": "result",
            "subtype": "error_during_execution" if is_error else "success",
            "is_error": is_error,
            "result": "Done: feature.txt says ok.",
            "session_id": "s1",
        },
        {"type": "system", "subtype": "task_summary", "detail": None},
    ]
    return "\n".join(json.dumps(x) for x in lines) + "\n"


class StreamingClaude(FakeClaude):
    """``FakeClaude`` whose develop seat streams. The seat still writes
    feature.txt for real; the stream is what the CLI would have printed."""

    def __init__(
        self,
        *,
        code: int = 0,
        is_error: bool = False,
        cut: bool = False,
        stderr: str = "",
        timed_out: bool = False,
        **kw,
    ):
        super().__init__(**kw)
        self.code, self.is_error, self.cut = code, is_error, cut
        self.stderr, self.timed_out = stderr, timed_out
        self.formats: list[list[str]] = []

    async def __call__(self, argv, *, cwd, timeout, stdin=None):
        if argv[0] != FAKE_CLAUDE or "Edit" not in argv[argv.index("--tools") + 1]:
            return await super().__call__(argv, cwd=cwd, timeout=timeout, stdin=stdin)
        fmt = argv[argv.index("--output-format") + 1 :]
        self.formats.append(fmt[:2])
        await super().__call__(argv, cwd=cwd, timeout=timeout, stdin=stdin)
        if fmt[:2] != ["stream-json", "--verbose"]:
            return 0, json.dumps({"type": "result", "result": "done"}), ""
        if self.timed_out:  # what run_subprocess returns after the kill
            return -1, "", f"timed out after {timeout:.0f}s"
        # The CLI reports the physical path, as ``getcwd`` does.
        cwd = Path(os.path.realpath(cwd))
        stderr = self.stderr.format(cwd=cwd)
        git_file = cwd / ".git"
        git_text = git_file.read_text() if git_file.is_file() else ""
        stream = _stream(cwd, is_error=self.is_error, cut=self.cut, git_text=git_text)
        return self.code, stream, stderr


def _linked_dir(tmp_path: Path) -> Path:
    """A dir reached through a symlink whose physical path ENDS with the link's
    path: the shape of macOS ``/var`` -> ``/private/var``, where stripping the
    shorter prefix first corrupts paths (``/privateREADME.md``)."""
    link = tmp_path / "v"
    real = Path(f"{tmp_path}/p{tmp_path}/v")
    real.mkdir(parents=True)
    link.symlink_to(real)
    assert os.path.realpath(link) == f"{tmp_path}/p{link}"
    return link


@pytest.fixture
def linked_tmp(tmp_path: Path, monkeypatch) -> Path:
    """Station temp dirs (``mkdtemp``) land under ``_linked_dir``."""
    link = _linked_dir(tmp_path)
    monkeypatch.setattr(tempfile, "tempdir", str(link))
    return link


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> InstinctStore:
    st = InstinctStore(tmp_path / "instinct.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: st)
    return st


async def _feed_doc(action_id: str):
    from pocketpaw_ee.cloud.models.belt_run_feed import BeltRunFeed

    return await BeltRunFeed.find_one(
        BeltRunFeed.workspace == "w1", BeltRunFeed.action_id == action_id
    )


# ---------------------------------------------------------------------------
# end to end: queued run -> station -> Mongo -> route
# ---------------------------------------------------------------------------


async def test_develop_feed_is_stored_in_order_and_served(
    repo,  # noqa: F811
    store,
    mongo_db,
    monkeypatch,
    linked_tmp,
):
    action_id = await _queue_run(monkeypatch, repo, recipe="")
    fake = StreamingClaude(develop=[_write("ok")])
    await HeadlessDevelopRunner(develop_fn=_station(fake, repo)).run(action_id)

    # The develop seat streamed; the run still attached its diff.
    assert fake.formats == [["stream-json", "--verbose"]]
    blob = (await store.get_action(action_id)).parameters["_code_change"]
    assert "+ok" in blob["diff"] and not blob.get("headless_error")

    doc = await _feed_doc(action_id)
    assert doc is not None and doc.stage == "develop" and doc.steps_omitted == 0
    shape = [(s["kind"], s["tool"] or s["text"]) for s in doc.steps]
    assert shape == [
        ("thinking", "The task wants a file.\n\nI'll add feature.txt."),
        ("tool", "Read"),
        ("tool", "Write"),
        ("tool", "Edit"),
        ("tool", "Read"),
        ("tool", "Bash"),
        ("tool", "Bash"),
        ("thinking", "Done: feature.txt says ok."),
    ]
    read, write, edit, git_read, cd, pwd = doc.steps[1:7]
    assert read["input"] == {"file_path": "README.md"}  # the worktree prefix is gone
    # Bare worktree paths read "." and the bound repo the .git file names goes too.
    assert git_read["output"].startswith("gitdir: .git/worktrees/")
    assert cd["input"] == {"command": "cd . && uv run pytest -q"} and "cd . &&" in cd["output"]
    assert pwd["output"] == "."
    stored_text = json.dumps(doc.steps, default=str)
    assert "belt-develop-" not in stored_text and str(linked_tmp) not in stored_text
    assert str(repo) not in stored_text
    assert write["output"] == "File created at feature.txt"
    assert edit["input"]["old_string"] == "draft" and edit["status"] == "complete"
    assert str(read["started_at"]).startswith("2026-10-06 08:00:02")

    with TestClient(_build_app(role="member")) as client:
        res = client.get(f"/api/v1/belt/runs/{action_id}/feed", params={"stage": "develop"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["action_id"] == action_id and body["stage"] == "develop"
    assert [s["tool"] for s in body["steps"]] == [
        "",
        "Read",
        "Write",
        "Edit",
        "Read",
        "Bash",
        "Bash",
        "",
    ]
    assert body["steps"][1]["startedAt"].startswith("2026-10-06T08:00:02")
    assert "stepsOmitted" not in body

    # The planted secrets (a tool result, a tool input) never reach either.
    stored = json.dumps([s for s in doc.steps], default=str)
    for secret in (_SECRET, _INPUT_SECRET):
        assert secret not in stored and secret not in res.text
    assert "[REDACTED]" in read["output"] and "[REDACTED]" in edit["input"]["new_string"]


async def test_stream_result_envelope_is_the_seat_result(repo):  # noqa: F811
    """The station reads the result line of a stream (not the trailing system
    line), and an ``is_error`` result still fails the seat."""
    station = _station(StreamingClaude(), repo)
    req = _request(repo)
    text = await station._claude("p", cwd=repo, step="DEVELOP", feed_for=req)
    assert text == "Done: feature.txt says ok."
    with pytest.raises(ds.DevelopStationError, match="DEVELOP: claude reported an error"):
        await _station(StreamingClaude(is_error=True), repo)._claude(
            "p", cwd=repo, step="DEVELOP", feed_for=req
        )
    # A run killed right after ``system/init`` is one JSON line, not a result.
    init = json.dumps({"type": "system", "subtype": "init", "cwd": "/x"})
    assert foreman.claude_result_envelope(init) is None
    assert ds._claude_said(init) == "no message"


@pytest.mark.parametrize(
    ("stderr", "said"),
    [
        ("", "I'll add feature.txt."),
        (
            "fatal: {cwd}/feature.txt: denied\nEACCES: scandir '{cwd}'",
            "fatal: feature.txt: denied\nEACCES: scandir '.'",
        ),
    ],
)
async def test_failed_develop_still_stores_its_feed(
    repo,  # noqa: F811
    store,
    mongo_db,
    monkeypatch,
    linked_tmp,
    stderr,
    said,
):
    action_id = await _queue_run(monkeypatch, repo, recipe="")
    fake = StreamingClaude(code=1, cut=True, stderr=stderr, develop=[_write("ok")])
    await HeadlessDevelopRunner(develop_fn=_station(fake, repo)).run(action_id)

    error = (await store.get_action(action_id)).parameters["_code_change"]["headless_error"]
    assert "DEVELOP: claude exited 1" in error
    # What claude said (stderr, else its last prose), never raw stream-json,
    # and no worktree path in either spelling.
    assert said in error
    assert '"type"' not in error
    assert "belt-develop-" not in error and str(linked_tmp) not in error
    doc = await _feed_doc(action_id)
    assert [s["tool"] for s in doc.steps] == ["", "Read", "Write"]
    # The call the CLI died in says so instead of spinning.
    assert doc.steps[-1]["status"] == "missing_result"


async def test_a_timed_out_redevelop_replaces_the_earlier_feed(repo, mongo_db):  # noqa: F811
    """The stage row is the latest attempt: a re-develop that times out (no
    stdout) leaves an empty feed, never the first attempt's steps."""
    from dataclasses import replace

    req = replace(_request(repo), action_id="run-x")
    await _station(StreamingClaude(develop=[_write("ok")]), repo)(req)
    assert len((await _feed_doc("run-x")).steps) == 8

    with pytest.raises(ds.DevelopStationError, match="DEVELOP: claude exited -1: timed out"):
        await _station(StreamingClaude(timed_out=True), repo)(req)
    doc = await _feed_doc("run-x")
    assert doc.steps == [] and doc.steps_omitted == 0


def test_parallel_same_name_calls_pair_by_id():
    """Two parallel Reads whose results arrive in the other order: each output
    lands on its own call (pairing by name would swap them)."""

    def use(i: str, path: str) -> dict:
        block = {"type": "tool_use", "id": i, "name": "Read", "input": {"file_path": path}}
        return {"type": "assistant", "timestamp": _t(1), "message": {"content": [block]}}

    def result(i: str, text: str) -> dict:
        block = {"type": "tool_result", "tool_use_id": i, "content": text}
        return {"type": "user", "timestamp": _t(2), "message": {"content": [block]}}

    lines = [use("t1", "a.txt"), use("t2", "b.txt"), result("t2", "B"), result("t1", "A")]
    recorder = fold_feed(stream_events("\n".join(json.dumps(x) for x in lines)))
    got = [(s["input"]["file_path"], s["output"]) for s in recorder.steps]
    assert got == [("a.txt", "A"), ("b.txt", "B")]


async def test_a_redevelop_that_fails_before_its_seat_clears_the_feed(repo, mongo_db):  # noqa: F811
    """A re-develop that dies in PREPARE (here the charter is gone) still
    clears the stage: the page never shows the earlier attempt as this one."""
    from dataclasses import replace

    req = replace(_request(repo), action_id="run-y")
    await _station(StreamingClaude(develop=[_write("ok")]), repo)(req)
    assert (await _feed_doc("run-y")).steps

    station = _station(StreamingClaude(), repo)

    async def no_charter(_w, _m):
        return None

    station.charter_for = no_charter
    with pytest.raises(ds.DevelopStationError, match="PREPARE: mandate"):
        await station(req)
    assert (await _feed_doc("run-y")).steps == []


async def test_a_check_tail_carries_no_worktree_path(repo, store, monkeypatch, linked_tmp):  # noqa: F811
    """A failing check that prints its cwd: the CHECK error on the run reads
    ``.``, not the temp dir."""
    import sys

    pwd_check = f'{sys.executable} -c "import os,sys; print(os.getcwd()); sys.exit(1)"'
    action_id = await _queue_run(monkeypatch, repo, recipe="")
    station = _station(StreamingClaude(develop=[_write("ok")]), repo, checks=(pwd_check,))
    await HeadlessDevelopRunner(develop_fn=station).run(action_id)

    error = (await store.get_action(action_id)).parameters["_code_change"]["headless_error"]
    assert "CHECK:" in error and "still failing" in error
    assert error.rstrip().endswith(".")
    assert "belt-develop-" not in error and str(linked_tmp) not in error


async def test_save_run_feed_upserts_per_workspace_and_keeps_created_at(mongo_db):
    """One row per (workspace, run, stage): an update keeps ``createdAt`` and
    replaces the steps; another workspace's same run/stage is its own row."""
    import asyncio

    from pocketpaw_ee.cloud.belt import service as belt_service
    from pocketpaw_ee.cloud.models.belt_run_feed import BeltRunFeed

    await belt_service.save_run_feed("w1", "a1", "develop", [{"kind": "tool", "tool": "Read"}], 0)
    first = await _feed_doc("a1")
    await asyncio.sleep(0.01)
    await belt_service.save_run_feed("w1", "a1", "develop", [{"kind": "tool", "tool": "Edit"}], 2)
    again = await _feed_doc("a1")
    assert again.id == first.id and again.createdAt == first.createdAt
    assert again.updatedAt > first.updatedAt
    assert [s["tool"] for s in again.steps] == ["Edit"] and again.steps_omitted == 2

    await belt_service.save_run_feed("w2", "a1", "develop", [{"kind": "tool", "tool": "Bash"}], 0)
    rows = await BeltRunFeed.find(BeltRunFeed.action_id == "a1").to_list()
    assert sorted((r.workspace, r.steps[0]["tool"]) for r in rows) == [
        ("w1", "Edit"),
        ("w2", "Bash"),
    ]


async def test_a_failing_save_never_fails_the_run(repo, store, monkeypatch, caplog):  # noqa: F811
    action_id = await _queue_run(monkeypatch, repo, recipe="")
    station = _station(StreamingClaude(develop=[_write("ok")]), repo)

    async def broken_save(*_a, **_k):
        raise RuntimeError("mongo down")

    station.save_feed = broken_save
    await HeadlessDevelopRunner(develop_fn=station).run(action_id)
    blob = (await store.get_action(action_id)).parameters["_code_change"]
    assert "+ok" in blob["diff"] and not blob.get("headless_error")
    assert "could not store the develop feed" in caplog.text


def test_worktree_paths_are_relative_whatever_the_prefix_order(tmp_path):
    """Both spellings of the worktree go, longest first, for many dir names: a
    set's iteration order differs per string, so a station that relied on it
    would mangle some of these (``/p...README.md``) in any process."""
    link = _linked_dir(tmp_path)
    for i in range(24):
        cwd = link / f"belt-develop-{i:02d}x" / "wt"
        cwd.mkdir(parents=True)
        physical = os.path.realpath(cwd)
        text = json.dumps(
            {
                "a": f"{physical}/README.md",
                "b": f"{cwd}/src/x.py",
                "c": f"cd {physical} && pwd",
                "d": str(cwd),
                "e": f"{cwd}-old/y",
            }
        )
        assert json.loads(ds._relative_paths(text, cwd)) == {
            "a": "README.md",
            "b": "src/x.py",
            "c": "cd . && pwd",
            "d": ".",
            "e": f"{cwd}-old/y",  # a sibling keeps its path
        }


# ---------------------------------------------------------------------------
# caps, and the route's tenancy
# ---------------------------------------------------------------------------


def _calls(n: int, output: str = "x") -> list[AgentEvent]:
    events: list[AgentEvent] = []
    for i in range(n):
        events.append(AgentEvent("tool_use", "Read", {"name": "Read", "input": {"i": i}}))
        events.append(AgentEvent("tool_result", output, {"name": "Read"}))
    return events


def _batched_calls(n: int) -> list[AgentEvent]:
    """``n`` parallel calls with ids, every result after every call, reversed."""
    uses = [
        AgentEvent("tool_use", "Read", {"name": "Read", "input": {"i": i}, "call_id": f"c{i}"})
        for i in range(n)
    ]
    results = [
        AgentEvent("tool_result", f"r{i}", {"name": "Read", "call_id": f"c{i}"})
        for i in reversed(range(n))
    ]
    return uses + results


def test_feed_caps_steps_and_counts_the_rest():
    recorder = fold_feed(_calls(FEED_MAX_STEPS + 25))
    assert len(recorder.steps) == FEED_MAX_STEPS
    assert recorder.steps_omitted == 25
    # Every kept call still has its own result (dropped calls' results don't
    # land on a kept one).
    assert all(s["status"] == "complete" and s["output"] == "x" for s in recorder.steps)

    # By id: the dropped calls' results are neither paired nor counted again.
    recorder = fold_feed(_batched_calls(FEED_MAX_STEPS + 25))
    assert len(recorder.steps) == FEED_MAX_STEPS and recorder.steps_omitted == 25
    assert all(s["output"] == f"r{s['input']['i']}" for s in recorder.steps)


def test_feed_caps_bytes():
    from pocketpaw_ee.cloud.belt.feed import FEED_MAX_BYTES

    recorder = fold_feed(_calls(1_000, output="y" * 4_000))
    size = sum(len(json.dumps(s, default=str)) for s in recorder.steps)
    assert size <= FEED_MAX_BYTES
    assert recorder.steps_omitted > 0
    assert len(recorder.steps) + recorder.steps_omitted == 1_000


async def test_feed_route_tenancy_and_empty(store, mongo_db):
    from pocketpaw_ee.cloud.belt import service as belt_service

    from tests.cloud.test_belt_console import _propose_run

    mine = await _propose_run(store, task="mine")
    foreign = await _propose_run(store, workspace_id="w-other", task="foreign")
    await belt_service.save_run_feed("w-other", foreign.id, "develop", [{"kind": "tool"}], 0)

    with TestClient(_build_app(role="member", workspace_id="w1")) as client:
        empty = client.get(f"/api/v1/belt/runs/{mine.id}/feed")
        cross = client.get(f"/api/v1/belt/runs/{foreign.id}/feed")
        bad = client.get(f"/api/v1/belt/runs/{mine.id}/feed", params={"stage": "../x"})
    assert empty.status_code == 200 and empty.json() == {"action_id": mine.id, "stage": "develop"}
    assert cross.status_code == 404, cross.text
    assert bad.status_code == 422
