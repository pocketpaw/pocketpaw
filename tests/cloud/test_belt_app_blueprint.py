# tests/cloud/test_belt_app_blueprint.py — the Belt blueprint's pure derivation.
#
# ``app_blueprint`` over trimmed copies of real creative-apps files (dev, and
# lead/layer-keys for the RECORD) in ``fixtures/app_blueprint/``: Halo's
# c4-planned.json, three wave.json files, the layer-keys RECORD (Decisions and
# Open cut) and dev's Halo file tree. The GitHub side (issue, run, PRs) is built
# in each test as it stood when the design doc was written: #1036 open, the lead
# run pending, no feature-done PR merged. A v3 wave.json's ``slices`` take their
# status off the lead run (done, done-with-gaps, building, planned).

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.belt import app_blueprint as ab  # noqa: E402
from pocketpaw_ee.cloud.belt.orient import load_model, path_index  # noqa: E402

FX = Path(__file__).parent / "fixtures" / "app_blueprint"
MODEL = load_model((FX / "halo-c4-planned.json").read_text())
TREE = (FX / "dev-tree.txt").read_text().split()
RECORD = (FX / "layer-keys-RECORD.md").read_text()
LAYER_KEYS = (FX / "halo-layer-keys.wave.json").read_text()
OPEN = {"state": "open", "labels": ["feature-lead", "line:halo"]}
RUNNING = {
    "action_id": "act-1a13eb691a3-wg4n",
    "status": "running",
    "lead": {"prs": [{"number": 1122, "state": "MERGED", "done": False}], "stalls": 0},
}
DONE_PR = {"number": 1129, "state": "MERGED", "done": True}
REJECTED = {"status": "rejected", "lead": {"stalls": 0}}


def derive(wave=LAYER_KEYS, record=RECORD, app="halo", fid="layer-keys", **kw):
    kw = {"issue": OPEN, "run": RUNNING, "tree": TREE, **kw}
    return ab.derive_feature(app, fid, wave, record, model=MODEL, **kw)


def test_layer_keys_is_building_with_each_task_on_its_layer():
    f = derive()
    assert (f["id"], f["status"], f["claimed"], f["wave"]) == ("halo/layer-keys",) + (
        "building",
    ) * 2 + ("1",)
    assert (f["issue"]["number"], f["depends"], f["waits_on"]) == (1036, ["foundation"], [777, 333])
    assert f["run"]["action_id"] == "act-1a13eb691a3-wg4n"
    assert f["run"]["stage"] == "5 finish (blocked: dev red on #1185, not ours)"
    assert f["run"]["next"].startswith("when #1185 merges")
    layer = {t["id"]: t["layer"] for t in f["tasks"]}
    assert (layer["LK-12"], layer["LK-9"], layer["LK-1"]) == ("tests", "app", "model")
    assert layer["LK-3"] == "model"  # "same as LK-2a" takes LK-2a's files
    lk12 = next(t for t in f["tasks"] if t["id"] == "LK-12")
    assert lk12 == {
        "id": "LK-12",
        "wave": "7",
        "layer": "tests",
        "pr": 1122,
        "state": "merged",
        "files": ["UITests/HaloJourneyTests+Drag.swift"],
    }
    assert f["layers"] == ["tests", "app", "model"]
    assert {"halo/halo-window", "halo/halo-services", "halo/halo-model-tests"} <= set(
        f["components"]
    )
    assert f["gaps"] == []


def test_record_task_files_resolve_against_the_dev_tree():
    rec = ab.parse_record(RECORD)
    lk1 = next(t for t in rec["tasks"] if t["id"] == "LK-1")
    assert ab.resolve_files(lk1["files"], TREE) == [
        "Sources/HaloModel/Live/HaloKeyRules.swift",
        "Sources/HaloModel/Live/LiveLayerPlacing.swift",
        "Sources/HaloModel/Fakes/FakeLayerPlacing.swift",
        "Tests/HaloModelTests/LayerPlacingContractTests.swift",
    ]
    assert len(rec["tasks"]) == 15  # the Rounds table is not read as tasks
    assert ab.resolve_files(["Keys"], ["A/HaloWindow+Keys.swift", "B/Keys.swift"]) == [
        "B/Keys.swift"
    ]
    off_dev = {t["id"]: t["layer"] for t in derive(tree=[])["tasks"]}  # owned paths resolve them
    assert (off_dev["LK-9"], off_dev["LK-12"]) == ("app", "tests")


def test_a_built_claim_with_no_done_pr_is_claimed_ahead():
    wave = (FX / "flare-live-keys.wave.json").read_text()
    f = derive(wave, None, app="flare", fid="live-keys", issue={"state": "open", "labels": []})
    assert (f["claimed"], f["status"]) == ("built", "gap") and "claimed_ahead" in f["gaps"]
    assert f["waits_on"] == [171, 216, 219]
    for pr in ({**DONE_PR, "state": "OPEN"}, {**DONE_PR, "done": False}):  # not a merged done PR
        assert "claimed_ahead" in derive(wave, None, app="flare", fid="live-keys", prs=[pr])["gaps"]
    f = derive(wave, None, app="flare", fid="live-keys", prs=[DONE_PR])
    assert (f["status"], f["gaps"]) == ("built", ["feature_no_owned"])


@pytest.mark.parametrize("text", [None, "", "not a record\x00", "# r\nNext: go\n## Tasks\n"])
def test_an_unreadable_record_gives_no_stage(text):
    rec = ab.parse_record(text)
    assert rec["stage"] is None and rec["tasks"] == []
    f = derive(record=text)
    assert f["run"]["stage"] is None and f["tasks"] == [] and f["status"] == "building"


def test_a_feature_with_no_owned_list_is_placed_by_its_record_tasks_else_unknown():
    no_owned = LAYER_KEYS.replace('"owned"', '"owned_was"')
    f = derive(no_owned)
    assert f["layers"] == ["tests", "app", "model"] and "feature_no_owned" in f["gaps"]
    assert "halo/halo-window" in f["components"]
    f = derive(no_owned, None)
    assert (f["layers"], f["components"]) == (["unknown"], [])
    one = LAYER_KEYS.replace('"Sources/HaloModel/Live/LiveLayerPlacing.swift",', "").split(
        '"owned"'
    )[0]
    one += '"owned": ["Sources/HaloApp/HaloWindow+Keys.swift"]}'
    assert derive(one)["layers"] == ["app"]  # owned wins over the RECORD tasks


@pytest.mark.parametrize(
    "status,issue,run,prs,claimed,num",
    [
        ("done", OPEN, {"status": "landed"}, [], "building", 1),
        ("done", {"state": "closed"}, None, [DONE_PR], "planned", 1),
        ("built", OPEN, RUNNING, [DONE_PR], "building", 1),
        ("built", OPEN, {"status": "running", "lead": {"prs": [DONE_PR]}}, [], "building", 1),
        ("gap", {**OPEN, "labels": ["needs-human"]}, None, [], "built", 1),
        ("gap", {"state": "CLOSED"}, None, [], "planned", 1),
        ("stuck", {**OPEN, "labels": ["needs-human"]}, RUNNING, [], "building", 1),
        ("stuck", OPEN, {"status": "running", "lead": {"stalls": 2}}, [], "building", 1),
        ("building", OPEN, {"status": "running", "lead": {"stalls": 1}}, [], "building", 1),
        ("stuck", OPEN, {"status": "rejected"}, [], "building", 1),
        # The issue's newest Belt-Contact-Sheet: / Mac-Finish-Wanted: line (the read's
        # ``mark``) splits the red: parked on the Mac waits, a released sheet is done.
        ("waiting", {**OPEN, "labels": ["needs-human"], "mark": "mac"}, None, [], "building", 1),
        ("waiting", {**OPEN, "labels": ["needs-human"], "mark": "mac"}, REJECTED, [], "planned", 1),
        ("done", {**OPEN, "mark": "sheet"}, REJECTED, [], "building", 1),
        ("done", {**OPEN, "labels": ["needs-human"], "mark": "sheet"}, REJECTED, [], "planned", 1),
        ("stuck", {**OPEN, "labels": ["needs-human"], "mark": None}, REJECTED, [], "building", 1),
        ("stuck", {**OPEN, "mark": "mac"}, {"status": "running", "lead": {"stalls": 3}}, [], "", 1),
        ("stuck", OPEN, {"status": "failed"}, [], "building", 1),
        ("building", OPEN, {"status": "queued"}, [], "planned", 1),
        ("filed", OPEN, None, [], "planned", 1),
        ("planned", None, None, [], "planned", None),
    ],
)
def test_status_first_rule_that_holds_wins(status, issue, run, prs, claimed, num):
    assert ab.feature_status(claimed, num, issue, run, prs)[0] == status


def test_issue_closed_without_done_is_a_gap_kind():
    closed = {"state": "closed"}
    assert ab.feature_status("planned", 1, closed, None, [])[1] == ["issue_closed_not_done"]
    assert ab.feature_status("done", 1, closed, None, [DONE_PR])[1] == []
    assert ab.feature_status("done", 1, OPEN, None, []) == ("gap", ["claimed_ahead"])


def test_feature_issue_must_be_a_number_to_count_as_filed():
    for raw, num in {
        "#1036": 1036,
        "file when ready": None,
        "later": None,
        "#12 soon": None,
    }.items():
        assert ab.parse_wave(f'{{"feature_issue": "{raw}"}}')["issue"] == num
    assert ab.parse_wave((FX / "halo-easing.wave.json").read_text())["waits_on"] == [333]
    assert ab.parse_wave("[1]") is None and ab.parse_wave("{") is None


LAYER_TABLE = """halo-tests:tests photo-journeys:tests halo-snapshots:tests app-audit:tests
suiteui-tests:tests suiteui:shell suiteui-kit:shell suite-shell:shell afterglow-dock:shell
halo-model:model suite-logic:model halo-app:app apps:app docs:other"""


@pytest.mark.parametrize("container,layer", [p.split(":") for p in LAYER_TABLE.split()])
def test_container_layer_first_match_wins(container, layer):
    assert ab.container_layer(container) == layer
    assert ab.container_layer(container, external=True) == "engine"


def test_task_layer_is_the_majority_of_its_non_test_files():
    index, layers = path_index(MODEL), ab.component_layers(MODEL)
    assert layers["flarecore-sources"] == "engine" and layers["suiteui-sources"] == "shell"

    def place(*files):
        return ab.task_layer(list(files), index, layers)

    app, model = "Sources/HaloApp/HaloWindow+Keys.swift", "Sources/HaloModel/HaloKeying.swift"
    assert place(app, "Tests/HaloModelTests/A.swift", "Tests/HaloModelTests/B.swift") == "app"
    assert place(model, app, "Sources/HaloApp/HaloLayerInspector.swift") == "app"
    snap, journey = "Tests/AppSnapshotTests/HaloX.swift", "UITests/HaloJourneyTests+X.swift"
    assert place(snap, journey) == "tests"  # no component owns them: the prefix rule places them
    assert place("Package.swift") == "other"
    assert place() == "unknown"


def test_issue_and_run_carry_every_contract_field():
    # #799 is not in the gh list: the row still has a state and labels.
    assert derive(issue=None)["issue"] == {"number": 1036, "state": "unknown", "labels": []}
    assert derive()["issue"] == {"number": 1036, **OPEN}
    # nothing read carries the RECORD's commit time yet
    assert derive()["run"]["record_at"] is None


# -- slices (factory v3): the wave.json ``slices`` and each one's status ------

SLICES = [
    {"id": "CT-a", "behavior": "protocols and fakes", "tasks": ["LK-1"], "journeys": []},
    {"id": "CT-b", "tasks": "LK-2a", "journeys": ["J5"], "waits": {"CT-a": ["LK-2a"]}},
    {"id": "CT-c", "tasks": ["LK-9"], "waits": {"design-window:DW-a": ["LK-9"], "seams": []}},
    {"id": "CT d", "tasks": ["LK-12"]},  # not a safe id
    {"behavior": "no id"},
    "not a slice",
]


def test_wave_slices_keep_entries_with_an_id_and_their_waits_keys():
    wave = ab.parse_wave(json.dumps({"slices": SLICES}))
    assert [s["id"] for s in wave["slices"]] == ["CT-a", "CT-b", "CT-c", "CT d"]
    assert wave["slices"][0] == {
        "id": "CT-a",
        "behavior": "protocols and fakes",
        "tasks": ["LK-1"],
        "journeys": [],
        "waits": [],
    }
    assert wave["slices"][1]["tasks"] == []  # a string is not a task list
    assert wave["slices"][2]["waits"] == ["design-window:DW-a", "seams"]
    assert ab.parse_wave("{}")["slices"] == ab.parse_wave('{"slices": {}}')["slices"] == []


def _sliced(**over) -> str:
    return json.dumps(json.loads(LAYER_KEYS) | {"slices": SLICES} | over)


def test_each_slice_takes_its_status_from_the_lead_run():
    lead = {
        "stalls": 0,
        "prs": [{"number": 1140, "state": "OPEN", "done": False, "slices": ["CT-c"]}],
        "slices": [
            {"id": "CT-a", "done": True, "run": "r1", "gaps": []},
            {"id": "CT-b", "done": True, "run": "r2", "gaps": [77]},
            {"id": "CT-c", "done": False, "run": None, "gaps": []},
        ],
    }
    f = derive(wave=_sliced(), run={**RUNNING, "lead": lead})
    got = [(s["id"], s["status"], s["run"], s["gaps"]) for s in f["slices"]]
    assert got == [
        ("CT-a", "done", "r1", []),
        ("CT-b", "done-with-gaps", "r2", [77]),
        ("CT-c", "building", None, []),  # an open PR names it; "CT d" is dropped
    ]


def test_slices_are_planned_without_a_live_lead_and_absent_without_any():
    assert {s["status"] for s in derive(wave=_sliced(), run=None)["slices"]} == {"planned"}
    prs = [{"number": 1140, "state": "MERGED", "done": False, "slices": ["CT-a"]}]
    released = {**RUNNING, "status": "rejected", "lead": {"stalls": 0, "prs": prs}}
    assert {s["status"] for s in derive(wave=_sliced(), run=released)["slices"]} == {"planned"}
    assert derive()["slices"] == []
