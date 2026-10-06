# tests/cloud/test_belt_live_feed.py — a Belt run's feed, tailed live.
#
# Pins the live half of the station feed (the stored half is test_belt_feed.py):
#   * the runner hands stdout lines over before the process exits, a line far
#     past the reader's 64 KiB limit included, with stdin fed alongside, and a
#     timeout still kills the group and returns no stdout.
#   * every station step (orient, develop, check, fix, review) publishes its
#     frames to the run's stream in order, in the chat frame vocabulary, each
#     tagged with its stage; the same frames are stored per stage.
#   * live frames carry no secret, worktree path, repo path or host account.
#   * the SSE route replays the newest attempt from its ``start``, 404s a
#     foreign run, and says ``from_history`` for a run with no stream that is
#     not being developed.

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

from fastapi.testclient import TestClient  # noqa: E402
from pocketpaw_ee.cloud.belt import develop_station as ds  # noqa: E402
from pocketpaw_ee.cloud.belt import feed as belt_feed  # noqa: E402
from pocketpaw_ee.cloud.belt import service as belt_service  # noqa: E402
from pocketpaw_ee.cloud.belt.headless import HeadlessDevelopRunner  # noqa: E402
from pocketpaw_ee.cloud.chat.runs.memory_stream import InMemoryStreamTransport  # noqa: E402

from tests.cloud.test_belt_console import _build_app, _propose_run  # noqa: E402
from tests.cloud.test_belt_develop_station import (  # noqa: E402
    CHECK,
    _queue_run,
    _station,
    _write,
    repo,  # noqa: F401 — the tmp git repo fixture
)
from tests.cloud.test_belt_feed import (  # noqa: E402
    _INPUT_SECRET,
    _SECRET,
    StreamingClaude,
    _feed_doc,
    linked_tmp,  # noqa: F401 — station temp dirs behind a symlink
    store,  # noqa: F401 — the instinct store fixture
)

PY = sys.executable
# Mandates here bind tmp repos outside the default allowlist roots.
pytestmark = pytest.mark.usefixtures("any_repo_root")


# ---------------------------------------------------------------------------
# the runner's on_line callback
# ---------------------------------------------------------------------------


async def test_lines_arrive_before_the_process_exits(tmp_path: Path):
    """Two lines half a second apart reach ``on_line`` half a second apart:
    they are read live, not after the process is gone."""
    script = (
        "import sys,time\n"
        "data=sys.stdin.read()\n"
        "print('first', len(data), flush=True)\n"
        "time.sleep(0.6)\n"
        "print('x'*200000, flush=True)\n"
        "sys.stdout.write('tail-without-newline'); sys.stdout.flush()\n"
    )
    seen: list[tuple[float, str]] = []

    async def on_line(line: str) -> None:
        seen.append((time.monotonic(), line))

    prompt = "p" * 300_000  # bigger than a pipe buffer: written alongside the reads
    code, out, _err = await ds.run_subprocess(
        [PY, "-c", script], cwd=tmp_path, timeout=30, stdin=prompt, on_line=on_line
    )
    assert code == 0
    assert [line[:5] for _, line in seen] == ["first", "x" * 5, "tail-"]
    assert seen[0][1] == "first 300000"
    assert len(seen[1][1]) == 200_000  # one line, past the 64 KiB readline limit
    assert seen[1][0] - seen[0][0] > 0.3, "lines were delivered only at exit"
    assert out == "\n".join(line for _, line in seen)


async def test_a_timed_out_streamed_run_returns_no_stdout(tmp_path: Path):
    seen: list[str] = []

    async def on_line(line: str) -> None:
        seen.append(line)

    script = "import time\nprint('started', flush=True)\ntime.sleep(30)\n"
    code, out, err = await ds.run_subprocess(
        [PY, "-c", script], cwd=tmp_path, timeout=1.5, on_line=on_line
    )
    assert (code, out) == (-1, "") and "timed out" in err
    assert seen == ["started"]  # what arrived before the kill is the live record


# ---------------------------------------------------------------------------
# the station: every stage, live and stored
# ---------------------------------------------------------------------------


@pytest.fixture
def transport(monkeypatch):
    """The in-memory run stream (the one ``get_stream_transport`` builds without
    Redis); its TTL timers are recorded instead of left running."""
    from pocketpaw_ee.cloud.chat.runs import transport as runs_transport

    monkeypatch.setenv("POCKETPAW_CLOUD_STREAM_TRANSPORT", "memory")
    runs_transport._reset_for_tests()
    ttls: list[tuple[str, int]] = []

    async def set_ttl(self, run_id: str, ttl_seconds: int) -> None:
        ttls.append((run_id, ttl_seconds))

    monkeypatch.setattr(InMemoryStreamTransport, "set_ttl", set_ttl)
    t = runs_transport.get_stream_transport()
    t.ttls = ttls
    yield t
    runs_transport._reset_for_tests()


def _entries(transport, action_id: str) -> list[tuple[str, str, dict]]:
    return list(transport._buffers[belt_feed.stream_id(action_id)].entries)


def _sse_events(body: bytes) -> list[tuple[str, dict]]:
    out = []
    for block in body.decode().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            out.append((lines["event"], json.loads(lines["data"])))
    return out


async def _body(stream) -> bytes:
    return b"".join([chunk async for chunk in stream])


async def test_every_stage_arrives_live_in_order_and_is_stored(
    repo,  # noqa: F811
    store,  # noqa: F811
    mongo_db,
    monkeypatch,
    linked_tmp,  # noqa: F811
    transport,
):
    """A run whose first develop fails its check: orient, develop, check, fix,
    check again, review, each frame published as the seat prints it, tagged
    with its stage, and each stage's steps stored in its own row. Each stage
    start says ``running`` at that stage on the bus and in the runs list, and
    the attached diff says ``proposed`` at the gate."""
    nudges: list[tuple[str, str]] = []
    rows_seen: list[tuple[str, str]] = []

    async def nudge(**kw):
        nudges.append((kw["status"], kw["stage"]))
        row = await belt_service.get_run(kw["workspace_id"], kw["action_id"])
        rows_seen.append((row["status"], row["stage"]))

    monkeypatch.setattr(belt_service, "emit_belt_run_updated", nudge)
    action_id = await _queue_run(monkeypatch, repo, recipe="")
    nudges.clear()  # filing the run nudged once already (queued / station)
    rows_seen.clear()
    fake = StreamingClaude(develop=[_write("nope"), _write("ok")])
    await HeadlessDevelopRunner(develop_fn=_station(fake, repo, checks=(CHECK,))).run(action_id)
    blob = (await store.get_action(action_id)).parameters["_code_change"]
    assert "+ok" in blob["diff"] and not blob.get("headless_error")

    frames = _entries(transport, action_id)
    names = [(e, d.get("stage"), d.get("round")) for _, e, d in frames if e in ("start", "stage")]
    assert names == [
        ("start", None, None),
        ("stage", "orient", 1),
        ("stage", "develop", 1),
        ("stage", "check", 1),
        ("stage", "fix", 1),
        ("stage", "check", 2),
        ("stage", "review", 1),
    ]
    assert frames[-1][1:] == ("stream_end", {"ok": True, "omitted": 0})
    live = [("running", s) for s in ("orient", "develop", "check", "fix", "check", "review")]
    assert nudges == [*live, ("proposed", "gate")]
    # The runs list agrees with each nudge as it is sent.
    assert rows_seen == nudges
    assert (await belt_service.get_run("w1", action_id))["headless_state"] is None
    # Every content frame says which stage it belongs to, in stage order.
    stage, seen = "", []
    for _, event, data in frames[1:-1]:
        if event == "stage":
            stage = data["stage"]
            continue
        assert data["stage"] == stage, (event, data)
        seen.append((stage, event, data.get("narration") or data.get("tool") or ""))
    assert ("orient", "tool_start", "Orient") in seen
    assert ("develop", "tool_start", "Read README.md") in seen
    assert ("develop", "tool_start", "Run cd . && uv run pytest -q") in seen
    assert ("fix", "tool_start", "Write feature.txt") in seen
    checks = [s for s in seen if s[0] == "check"]
    assert [c[1] for c in checks] == ["tool_start", "tool_result"] * 2
    results = [d for _, e, d in frames if e == "tool_result" and d["stage"] == "check"]
    assert results[0]["output"].endswith("NEED OK IN feature.txt\n(exit 1)")
    assert results[1]["output"].endswith("feature ok\n(exit 0)")
    # The live frames are scrubbed before they are sent: no secret, no
    # worktree or repo path, either spelling.
    sent = json.dumps(frames)
    for leak in (_SECRET, _INPUT_SECRET, "belt-develop-", str(linked_tmp), str(repo)):
        assert leak not in sent, leak
    assert "[REDACTED]" in sent
    assert transport.ttls == [
        (belt_feed.stream_id(action_id), 6 * 3600),
        (belt_feed.stream_id(action_id), 3600),
    ]

    # Each stage has its own stored row; check and fix keep both rounds.
    rows = {s: await _feed_doc(action_id, s) for s in belt_feed.STAGES}
    assert [rows["orient"].steps[0]["tool"]] == ["Orient"]
    assert len(rows["develop"].steps) == 9
    assert len(rows["check"].steps) == 2
    assert all(s["narration"].startswith("Run ") for s in rows["check"].steps)
    assert [s["tool"] for s in rows["fix"].steps] == [s["tool"] for s in rows["develop"].steps]
    # The verdict is a row of its own (the fake reviewer's seat prints no events).
    verdict = [(s["tool"], s["narration"], s["output"]) for s in rows["review"].steps]
    assert verdict == [("Review", "Review: pass", "no notes")]
    assert ("review", "tool_start", "Review: pass") in seen
    stored = json.dumps([r.steps for r in rows.values()], default=str)
    assert _SECRET not in stored and str(linked_tmp) not in stored

    # A reload replays the same frames through the route's generator.
    replay = _sse_events(await _body(await belt_service.open_run_stream("w1", action_id)))
    assert replay == [(e, d) for _, e, d in frames]


async def test_a_recipe_run_has_no_orient_fix_or_review(
    repo,  # noqa: F811
    store,  # noqa: F811
    mongo_db,
    monkeypatch,
    transport,
):
    import getpass

    action_id = await _queue_run(monkeypatch, repo, recipe="add-ok")
    me = getpass.getuser()
    write = f"open('feature.txt','w').write('ok\\\\n'); print('wrote')  # /home/{me}/x"
    recipe = f'{sys.executable} -c "{write}"'
    station = _station(StreamingClaude(), repo, checks=(CHECK,), recipes={"add-ok": recipe})
    await HeadlessDevelopRunner(develop_fn=station).run(action_id)
    frames = _entries(transport, action_id)
    stages = [d["stage"] for _, e, d in frames if e == "stage"]
    assert stages == ["develop", "check"]
    work = [d for _, e, d in frames if e == "tool_result" and d["stage"] == "develop"]
    assert work[0]["output"] == "wrote\n(exit 0)"
    starts = [d for _, e, d in frames if e == "tool_start"]
    assert starts[0]["narration"].startswith("Run ")
    # The command is scrubbed like seat output: the host account reads user.
    assert "/home/user/x" in starts[0]["input"]["command"]
    assert f"/home/{me}/" not in json.dumps(frames)


async def test_a_failed_run_still_ends_its_stream(repo, store, mongo_db, monkeypatch, transport):  # noqa: F811
    """A run that dies in a seat ends with ``stream_end {ok: false}``, so a
    viewer never waits on it, and goes back to ``queued`` at the station (its
    ``headless_error`` says why) on the bus and in the runs list."""
    nudges: list[tuple[str, str]] = []

    async def nudge(**kw):
        nudges.append((kw["status"], kw["stage"]))

    monkeypatch.setattr(belt_service, "emit_belt_run_updated", nudge)
    action_id = await _queue_run(monkeypatch, repo, recipe="")
    fake = StreamingClaude(code=1, cut=True)
    await HeadlessDevelopRunner(develop_fn=_station(fake, repo)).run(action_id)
    frames = _entries(transport, action_id)
    assert frames[-1][1:] == ("stream_end", {"ok": False, "omitted": 0})
    blob = (await store.get_action(action_id)).parameters["_code_change"]
    assert "DEVELOP: claude exited 1" in blob["headless_error"]
    assert nudges[-1] == ("queued", "station")
    row = await belt_service.get_run("w1", action_id)
    assert (row["status"], row["stage"], row["headless_state"]) == ("queued", "station", None)


async def test_a_broken_transport_never_fails_the_run(repo, store, mongo_db, monkeypatch):  # noqa: F811
    calls = []

    async def broken(*a, **k):
        calls.append(a)
        raise ConnectionError("redis is down")

    monkeypatch.setattr(belt_feed, "_publish", broken)
    action_id = await _queue_run(monkeypatch, repo, recipe="")
    fake = StreamingClaude(develop=[_write("ok")])
    await HeadlessDevelopRunner(develop_fn=_station(fake, repo)).run(action_id)
    blob = (await store.get_action(action_id)).parameters["_code_change"]
    assert "+ok" in blob["diff"] and not blob.get("headless_error")
    assert len(calls) == 1  # the first failure turns live publishing off
    assert (await _feed_doc(action_id)).steps  # the stored rows still land


async def test_the_live_frame_cap_counts_what_it_drops(monkeypatch):
    """Past ``FEED_MAX_STEPS`` frames in one call nothing more is sent, but the
    terminal ``stream_end`` always is, with the count."""
    sent: list[tuple[str, dict]] = []

    async def publish(_aid, event, data):
        sent.append((event, data))

    async def nothing(*_a, **_k):
        return None

    monkeypatch.setattr(belt_feed, "_publish", publish)
    monkeypatch.setattr(belt_feed, "_notify", nothing)
    feed = belt_feed.RunFeed("w1", "run-cap", nothing)
    await feed.start()
    await feed.stage("develop")
    for _ in range(belt_feed.FEED_MAX_STEPS + 5):
        await feed.add("thinking", {"content": "x"})
    await feed.end(True)
    assert [e for e, _ in sent].count("thinking") == belt_feed.FEED_MAX_STEPS
    assert sent[-1] == ("stream_end", {"ok": True, "omitted": 5})


# ---------------------------------------------------------------------------
# the SSE route
# ---------------------------------------------------------------------------


async def test_replay_serves_only_the_newest_attempt(store, mongo_db, transport):  # noqa: F811
    run = await _propose_run(store, task="mine")
    sid = belt_feed.stream_id(run.id)
    for n in (1, 2):
        await transport.append_event(sid, "start", {})
        await transport.append_event(sid, "stage", {"stage": "develop", "round": 1})
        await transport.append_event(
            sid, "thinking", {"content": f"attempt {n}", "stage": "develop"}
        )
        await transport.append_event(sid, "stream_end", {"ok": n == 2, "omitted": 0})
    events = _sse_events(await _body(await belt_service.open_run_stream("w1", run.id)))
    assert [e for e, _ in events] == ["start", "stage", "thinking", "stream_end"]
    assert events[2][1]["content"] == "attempt 2" and events[3][1]["ok"] is True

    # A cursor resumes after it: the first attempt's end is followed by the second.
    first_end = transport._buffers[sid].entries[3][0]
    resumed = _sse_events(await _body(await belt_service.open_run_stream("w1", run.id, first_end)))
    assert [d.get("content") for e, d in resumed if e == "thinking"] == ["attempt 2"]


async def test_a_run_with_no_stream_reads_from_history_unless_it_is_developing(
    store,  # noqa: F811
    mongo_db,
    transport,
):
    import asyncio

    run = await _propose_run(store, task="done long ago")
    events = _sse_events(await _body(await belt_service.open_run_stream("w1", run.id)))
    assert events == [("stream_end", {"from_history": True})]

    # Being developed (the dispatcher marked it) but not started: the stream
    # waits for the station's first frame instead of ending.
    params = dict(run.parameters)
    params["_code_change"] = {**params["_code_change"], "headless_state": "queued"}
    await store.update_parameters(run.id, params)
    stream = await belt_service.open_run_stream("w1", run.id)
    sid = belt_feed.stream_id(run.id)

    async def station():
        await asyncio.sleep(0.05)
        await transport.append_event(sid, "start", {})
        await transport.append_event(sid, "stream_end", {"ok": True, "omitted": 0})

    task = asyncio.create_task(station())
    async with asyncio.timeout(10):
        events = _sse_events(await _body(stream))
    await task
    assert [e for e, _ in events] == ["start", "stream_end"]


async def test_the_stream_route_404s_a_foreign_run(store, mongo_db, transport):  # noqa: F811
    foreign = await _propose_run(store, workspace_id="w-other", task="foreign")
    with TestClient(_build_app(role="member", workspace_id="w1")) as client:
        res = client.get(f"/api/v1/belt/runs/{foreign.id}/stream")
    assert res.status_code == 404, res.text
    assert "belt.run_not_found" in res.text


async def test_the_stream_route_serves_sse(store, mongo_db, transport):  # noqa: F811
    run = await _propose_run(store, task="mine")
    with TestClient(_build_app(role="member", workspace_id="w1")) as client:
        res = client.get(f"/api/v1/belt/runs/{run.id}/stream")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    assert _sse_events(res.content) == [("stream_end", {"from_history": True})]
