# tests/cloud/test_belt_mandate_model.py — a mandate line's Claude model.
#
# The charter's optional ``model`` (``fable`` / ``opus`` / ``sonnet``) picks the
# model every cloud session of the line starts on. Pinned here: create and GET
# round-trip it, a model outside the three is refused; the cloud launch puts
# ``--model <model>`` before ``--cloud <brief>`` (the brief stays last) and an
# unset model leaves the argv as it always was; a relaunched lead starts on the
# same model while a nudge into a live session carries none (``claude -p
# --cloud <id>`` only posts a message; the session keeps its creation model).
# ``PATCH /belt/mandates/{id}/charter`` changes only the model and the two
# budget caps: an omitted field stays, ``model: null`` clears it, anything else
# in the body or out of range is a 422, and the change survives a reload.

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("mongomock_motor")

from pocketpaw_ee.cloud.belt import cloud_station  # noqa: E402

from tests.cloud import test_belt_cloud_station as base  # noqa: E402
from tests.cloud import test_belt_lead_runs as leads  # noqa: E402
from tests.cloud.test_belt_cloud_station import (  # noqa: E402
    WS,
    FakeRunner,
    _dispatcher,
    _queue,
)
from tests.cloud.test_belt_crew import _client, _create  # noqa: E402

store, dirs, playbook = base.store, base.dirs, leads.playbook

pytestmark = pytest.mark.usefixtures("any_repo_root")


def _charter(model: str | None):
    async def charter_for(workspace_id: str, mandate_id: str) -> dict:
        extra = {"model": model} if model is not None else {}
        return {"repo": "x", "charter": {"checks": ["make ci"], **extra}}

    return charter_for


def _launches(runner) -> list[list[str]]:
    return [a for a, _ in runner.calls if "--cloud" in a and "-p" not in a]


# ---------------------------------------------------------------------------
# launch argv
# ---------------------------------------------------------------------------


async def test_a_charter_model_rides_the_cloud_launch(store, dirs):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = FakeRunner(launch_out="no session")
    d = _dispatcher(runner, launch)
    d.charter_for = _charter("fable")

    await d.run(run_ref, workspace_id=WS)

    (argv,) = _launches(runner)
    assert argv[1] == cloud_station.PTY_RUN
    assert argv[3:6] == ["--model", "fable", "--cloud"]
    assert "Closes #93" in argv[-1]  # the brief is still the last argument


async def test_no_charter_model_leaves_the_launch_argv_as_it_was(store, dirs):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = FakeRunner(launch_out="no session")
    d = _dispatcher(runner, launch)
    d.charter_for = _charter(None)

    await d.run(run_ref, workspace_id=WS)

    (argv,) = _launches(runner)
    assert "--model" not in argv
    assert argv[3] == "--cloud" and len(argv) == 5


async def test_a_relaunched_lead_keeps_the_model_and_a_nudge_sends_none(store, dirs, playbook):
    repo, launch = dirs
    run_ref = await leads._queue_lead(store, repo)
    runner = leads.LeadRunner()
    d = leads._lead_dispatcher(runner, launch, session_end=leads._ended)
    d.charter_for = _charter("fable")

    await d.run(run_ref, workspace_id=WS)

    first, again = _launches(runner)
    assert first[3:5] == ["--model", "fable"] and again[3:5] == ["--model", "fable"]

    runner = leads.LeadRunner()
    run_ref = await leads._queue_lead(store, repo)
    d = leads._lead_dispatcher(runner, launch)
    d.charter_for = _charter("fable")
    await d.run(run_ref, workspace_id=WS)
    nudges = leads._nudges(runner)
    assert nudges and all("--model" not in a for a in nudges)


# ---------------------------------------------------------------------------
# create / GET / PATCH
# ---------------------------------------------------------------------------


def _create_with(client, repo_dir, **charter) -> tuple[int, dict]:
    res = client.post(
        "/belt/mandates",
        json={
            "name": "metal line",
            "surface": {"repo_id": str(repo_dir)},
            "charter": {"goal": "a Metal renderer", "cadence": "manual", **charter},
        },
    )
    return res.status_code, res.json()


async def test_create_round_trips_the_model_and_refuses_another(tmp_path, mongo_db, monkeypatch):
    client = _client(monkeypatch)
    code, body = _create_with(client, tmp_path, model="fable")
    assert code == 200, body
    mid = body["mandate"]["id"]
    assert client.get(f"/belt/mandates/{mid}").json()["charter"]["model"] == "fable"

    assert _create(client, tmp_path)["charter"]["model"] is None  # unset = today's behaviour
    code, _ = _create_with(client, tmp_path, model="gpt-4o")
    assert code == 422


async def test_patch_charter_sets_model_and_caps_only(tmp_path, mongo_db, monkeypatch):
    client = _client(monkeypatch)
    mandate = _create(client, tmp_path)
    mid = mandate["id"]

    res = client.patch(
        f"/belt/mandates/{mid}/charter",
        json={"model": "fable", "budget": {"max_in_flight": 1, "max_tasks_per_shift": 2}},
    )
    assert res.status_code == 200, res.text
    charter = res.json()["mandate"]["charter"]
    assert charter["model"] == "fable"
    assert charter["budget"]["max_in_flight"] == 1
    assert charter["budget"]["max_tasks_per_shift"] == 2
    # Nothing else moved.
    before = {k: v for k, v in mandate["charter"].items() if k not in {"model", "budget"}}
    assert {k: v for k, v in charter.items() if k not in {"model", "budget"}} == before
    assert (
        charter["budget"]["gate_minutes_per_week"]
        == (mandate["charter"]["budget"]["gate_minutes_per_week"])
    )

    # An omitted field stays; the change survives a reload from Mongo.
    res = client.patch(f"/belt/mandates/{mid}/charter", json={"budget": {"max_in_flight": 3}})
    assert res.status_code == 200, res.text
    got = client.get(f"/belt/mandates/{mid}").json()["charter"]
    assert got["model"] == "fable" and got["budget"]["max_in_flight"] == 3
    assert got["budget"]["max_tasks_per_shift"] == 2

    # ``model: null`` clears it back to the default launch.
    res = client.patch(f"/belt/mandates/{mid}/charter", json={"model": None})
    assert res.status_code == 200, res.text
    assert client.get(f"/belt/mandates/{mid}").json()["charter"]["model"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"model": "gpt-4o"},
        {"model": "haiku"},
        {"budget": {"max_in_flight": 0}},
        {"budget": {"max_in_flight": 21}},
        {"budget": {"max_tasks_per_shift": 0}},
        {"budget": {"max_tasks_per_shift": None}},
        # A valid field beside an out-of-scope one: the whole body is refused.
        {"budget": {"max_in_flight": 2, "gate_minutes_per_week": 5}},
        {"model": "fable", "goal": "something else"},
        {"budget": {"gate_minutes_per_week": 5}},
        {"goal": "something else"},
        {},
    ],
)
async def test_patch_charter_refuses_bad_or_out_of_scope_bodies(
    tmp_path, mongo_db, monkeypatch, body
):
    client = _client(monkeypatch)
    mid = _create(client, tmp_path)["id"]

    res = client.patch(f"/belt/mandates/{mid}/charter", json=body)

    assert res.status_code == 422, res.text
    charter = client.get(f"/belt/mandates/{mid}").json()["charter"]
    assert charter["model"] is None and charter["budget"]["max_in_flight"] is None


async def test_patch_charter_on_another_workspaces_mandate_is_404(tmp_path, mongo_db, monkeypatch):
    mid = _create(_client(monkeypatch), tmp_path)["id"]
    other = _client(monkeypatch, workspace_id="w2")

    res = other.patch(f"/belt/mandates/{mid}/charter", json={"model": "fable"})

    assert res.status_code == 404


async def test_patch_charter_needs_belt_manage(tmp_path, mongo_db, monkeypatch):
    mid = _create(_client(monkeypatch), tmp_path)["id"]
    member = _client(monkeypatch)
    from types import SimpleNamespace

    from pocketpaw_ee.cloud.auth import current_active_user

    user = SimpleNamespace(
        id="u2", active_workspace=WS, workspaces=[SimpleNamespace(workspace=WS, role="member")]
    )

    async def _member():
        return user

    member.app.dependency_overrides[current_active_user] = _member

    res = member.patch(f"/belt/mandates/{mid}/charter", json={"model": "fable"})

    assert res.status_code == 403
