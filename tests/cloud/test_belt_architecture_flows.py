# tests/cloud/test_belt_architecture_flows.py — ``architecture_flows``: a repo's
# ``docs/c4/flows.json`` checked against its C4 model for ``GET /belt/architecture``.
# ``check`` keeps a valid flow and marks each hop ``implied`` or not (``reverse``
# read along ``to -> from``), drops a flow naming an unknown id with warnings, and
# survives non-dict model elements. ``load_flows`` runs on real git: no file is
# ``([], [])``; a git failure, a folder or an oversized blob is a warning. The
# hand-written schema check drops a flow off the schema, turns a malformed file
# into one warning, and agrees with flows.schema.json (jsonschema, test-only) on
# good and bad fixtures.

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.belt import architecture_flows as af  # noqa: E402
from pocketpaw_ee.cloud.belt import architecture_read as ar  # noqa: E402

from tests.cloud.test_belt_architecture_read import Origin, read  # noqa: E402

_WEB = {"id": "web", "components": [{"id": "ui"}]}
_APP = {"id": "app", "containers": [_WEB, {"id": "api"}, {"id": "db"}]}
_RELS = [("user", "web"), ("web", "api"), ("api", "db")]
MODEL = {
    "scope": "app",
    "model": {
        "people": [{"id": "user"}],
        "systems": [_APP],
        "relationships": [{"source": s, "target": t} for s, t in _RELS],
    },
}
FLOW = {
    "id": "save",
    "name": "Saving a draft",
    "steps": [
        {"from": "user", "to": "web", "label": "clicks Save"},
        {"at": "ui", "label": "debounces the edit"},
        {"from": "web", "to": "api", "label": "PUT /draft", "technology": "HTTPS"},
        {"note": "A failed write keeps the draft local."},
    ],
}


def _flow(*steps: dict, id: str = "f") -> dict:
    return {"id": id, "name": id, "steps": list(steps)}


def test_a_valid_flow_passes_with_every_real_hop_explicit():
    [flow], warnings = af.check({"flows": [FLOW]}, MODEL)
    implied = [s.get("implied") for s in flow["steps"]]
    assert warnings == [] and implied == [False, None, False, None]
    assert flow["steps"][1] == FLOW["steps"][1] and flow["steps"][3] == FLOW["steps"][3]


def test_a_reverse_step_is_the_reply_along_the_forward_relationship():
    step = {"from": "api", "to": "web", "label": "200 OK"}
    [got], _ = af.check({"flows": [_flow(step, {**step, "reverse": True})]}, MODEL)
    assert [s["implied"] for s in got["steps"]] == [True, False]  # only web -> api exists


def test_a_hop_with_no_relationship_is_kept_as_implied():
    flows, warnings = af.check(
        {"flows": [_flow({"from": "user", "to": "db", "label": "x"})]}, MODEL
    )
    assert warnings == [] and flows[0]["steps"][0]["implied"] is True


def test_a_flow_with_an_unknown_end_is_dropped_and_warned_others_kept():
    bad = _flow(
        {"from": "web", "to": "cache", "label": "get"}, {"at": "ghost", "label": "x"}, id="b"
    )
    flows, warnings = af.check({"flows": [bad, FLOW]}, MODEL)
    assert [f["id"] for f in flows] == ["save"]
    assert warnings == [
        {"flow": "b", "step": 0, "problem": "to 'cache' is not in the model"},
        {"flow": "b", "step": 1, "problem": "at 'ghost' is not in the model"},
    ]


def test_a_model_with_non_dict_elements_still_checks_flows():
    app = {"id": "app", "containers": ["x", {"id": "web", "components": [7]}]}
    rels = ["junk", {"source": "app", "target": "web"}]
    junk = {"model": {"people": "nobody", "systems": ["junk", app], "relationships": rels}}
    flow, ghost = (
        _flow({"from": "app", "to": "web", "label": "x"}),
        _flow({"at": "api", "label": "x"}, id="g"),
    )
    flows, warnings = af.check({"flows": [flow, ghost]}, junk)
    assert [f["id"] for f in flows] == ["f"] and flows[0]["steps"][0]["implied"] is False
    assert warnings == [{"flow": "g", "step": 0, "problem": "at 'api' is not in the model"}]


def _commit(work: Path, text: str, rel: str = "docs/c4/flows.json") -> str:
    (work / rel).write_text(text)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "add", "-A"], cwd=work, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "f"], cwd=work, check=True)
    out = subprocess.run([*git, "rev-parse", "HEAD"], cwd=work, check=True, capture_output=True)
    return out.stdout.decode().strip()


async def test_load_flows_reads_the_sha_and_a_missing_file_is_empty(tmp_path):
    o = Origin(tmp_path)
    bare = o.push()
    assert await af.load_flows(o.work, bare, MODEL) == ([], [])
    sha = _commit(o.work, json.dumps({"flows": [FLOW]}))
    flows, warnings = await af.load_flows(o.work, sha, MODEL)
    assert [f["id"] for f in flows] == ["save"] and warnings == []
    flows, warnings = await af.load_flows(o.work, _commit(o.work, "{not json"), MODEL)
    assert flows == [] and warnings[0]["problem"].startswith("flows.json is not JSON")


async def test_any_other_git_failure_or_an_oversized_blob_is_a_warning(tmp_path, monkeypatch):
    o = Origin(tmp_path)
    o.push()
    _, warnings = await af.load_flows(o.work, "0" * 40, MODEL)  # no such commit
    assert warnings[0]["problem"].startswith("flows.json is unreadable")
    sha = _commit(o.work, json.dumps({"flows": [FLOW]}))
    monkeypatch.setattr(af, "MAX_MODEL_BYTES", 10)
    _, warnings = await af.load_flows(o.work, sha, MODEL)
    assert warnings == [{"flow": None, "step": None, "problem": "flows.json is over 10 bytes"}]
    path = o.work / "docs" / "c4" / "flows.json"
    path.unlink()
    path.mkdir()
    tree = _commit(o.work, "", "docs/c4/flows.json/x")  # flows.json is now a folder
    _, warnings = await af.load_flows(o.work, tree, MODEL)
    assert warnings[0]["problem"] == "flows.json is a tree, not a file"


async def test_the_read_serves_flows_and_warnings_past_a_non_dict_system(tmp_path, monkeypatch):
    from pocketpaw_ee.cloud.belt import app_blueprint_read as abr

    for name, value in (("_CACHE", {}), ("_LOCKS", {}), ("_TASKS", {})):
        monkeypatch.setattr(ar, name, value)
    monkeypatch.setattr(abr, "_lead_runs", AsyncMock(return_value=[]))
    o = Origin(tmp_path)
    monkeypatch.setattr(ar, "_re_resolve_repo", lambda r: (o.clone.resolve(), None))
    (o.work / "docs" / "c4").mkdir(parents=True)
    ghost = _flow({"at": "nowhere", "label": "x"}, id="ghost")
    (o.work / "docs" / "c4" / "flows.json").write_text(json.dumps({"flows": [FLOW, ghost]}))
    o.push({**MODEL, "model": {**MODEL["model"], "systems": ["junk", _APP]}})
    o.cloned()
    body = await read()
    await asyncio.gather(*list(ar._TASKS.values()))  # the live fill runs behind the first read
    assert body["stale"] is False and [f["id"] for f in body["flows"]] == ["save"]
    assert body["warnings"] == [
        {"flow": "ghost", "step": 0, "problem": "at 'nowhere' is not in the model"}
    ]


def test_a_flow_off_the_schema_is_dropped_and_warned():
    mixed = _flow({"at": "web", "to": "api", "label": "?"}, id="mixed")
    flows, warnings = af.check({"flows": [mixed, {"name": "no id", "steps": []}, FLOW]}, MODEL)
    assert [f["id"] for f in flows] == ["save"]
    assert [(w["flow"], w["step"]) for w in warnings] == [("mixed", None), (None, None)]
    assert all(w["problem"].startswith("flows/") for w in warnings)


@pytest.mark.parametrize("data", [[], {"flows": "x"}, {"other": []}])
def test_a_malformed_file_is_one_warning_never_a_raise(data):
    problem = 'flows.json: must be {"flows": [...]}'
    assert af.check(data, MODEL) == ([], [{"flow": None, "step": None, "problem": problem}])


_MSG = {"from": "a", "to": "b", "label": "x"}
GOOD_FLOWS = [
    {"id": "f", "name": "n", "steps": [_MSG]},
    {
        "id": "f",
        "name": "n",
        "description": "",
        "steps": [{"note": "n"}, {"at": "a", "label": "l"}],
    },
    {"id": "f", "name": "n", "steps": [{**_MSG, "technology": "", "reverse": False}]},
    {"id": "i" * 200, "name": "n" * 2000, "steps": [{**_MSG, "technology": "t" * 200}] * 200},
]
BAD_FLOWS = [
    "flow",
    {"name": "n", "steps": [_MSG]},
    {"id": "", "name": "n", "steps": [_MSG]},
    {"id": "i" * 201, "name": "n", "steps": [_MSG]},
    {"id": "f", "name": "n" * 2001, "steps": [_MSG]},
    {"id": "f", "name": "n", "steps": []},
    {"id": "f", "name": "n", "steps": [_MSG] * 201},
    {"id": "f", "name": "n", "steps": {"0": _MSG}},
    {"id": "f", "name": "n", "steps": [_MSG], "extra": 1},
    {"id": 3, "name": "n", "steps": [_MSG]},
    {"id": "f", "name": "n", "description": 5, "steps": [_MSG]},
    {"id": "f", "name": "n", "steps": ["step"]},
    {"id": "f", "name": "n", "steps": [{"from": "a", "label": "x"}]},
    {"id": "f", "name": "n", "steps": [{**_MSG, "reverse": "yes"}]},
    {"id": "f", "name": "n", "steps": [{**_MSG, "technology": "t" * 201}]},
    {"id": "f", "name": "n", "steps": [{**_MSG, "at": "a"}]},
    {"id": "f", "name": "n", "steps": [{"at": "a", "label": "l", "note": "n"}]},
    {"id": "f", "name": "n", "steps": [{"at": "a"}]},
    {"id": "f", "name": "n", "steps": [{"note": ""}]},
    {"id": "f", "name": "n", "steps": [{"note": "n", "label": "l"}]},
    {"id": "f", "name": "n", "steps": [{**_MSG, "from": ""}]},
    {"id": "f", "name": "n", "steps": [{**_MSG, "label": None}]},
]


def test_the_hand_check_agrees_with_the_published_schema():
    from jsonschema import Draft202012Validator

    schema = json.loads(Path(af.__file__).with_name("flows.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    for flow in GOOD_FLOWS + BAD_FLOWS:
        by_schema = validator.is_valid({"flows": [flow]})
        by_hand = af._flow_problem(flow, "flows/0") is None
        assert by_schema == by_hand == (flow in GOOD_FLOWS), flow
    for doc in [{"flows": []}, {"flows": [], "x": 1}, [], {"flows": "x"}, {"other": []}, None]:
        assert validator.is_valid(doc) == (af.check(doc, MODEL)[1] == []), doc
