# tests/cloud/test_belt_app_blueprint_read.py — the blueprint readers and cache.
#
# ``read_blueprint`` over fake git and gh runners serving the B2 fixtures as a
# creative-apps clone (Halo's c4-planned.json, two wave files, the layer-keys
# RECORD on its lead branch); ``Fake.lead`` adds more leads. Pins the cache (no
# calls within the TTL; a moved lead head re-reads only its RECORD), which RECORDs
# a read fetches (only a lead in a wave file that is not closed and done) and how
# many at once, the stale fallback on a rate limit, and the guards (a bad id or an
# oversized file is refused before it is read). The
# run rows, the grep output and the blueprint .md are shaped like the real ones:
# ``stalls`` is an int, journey ids sit in a wave's ``note`` and the .md's
# Journeys section, a test function follows its ``///`` comment. A feature's
# slices and the app's slice counts come through the same read.
# ``read_suite_map`` joins docs/blueprints/suite/feature-map.json off the same
# cache entry; a missing or broken map is a 404 and never fails the graph.

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.belt import app_blueprint_read as abr  # noqa: E402
from pocketpaw_ee.cloud.belt.service import MAX_MODEL_BYTES, BeltConsoleError  # noqa: E402

FX = Path(__file__).parent / "fixtures" / "app_blueprint"
BP = "docs/blueprints/halo"
FILES = {
    f"{BP}/c4-planned.json": (FX / "halo-c4-planned.json").read_text(),
    f"{BP}/features/layer-keys.wave.json": (FX / "halo-layer-keys.wave.json").read_text(),
    f"{BP}/features/live-keys.wave.json": (FX / "flare-live-keys.wave.json").read_text(),
    f"{BP}/features/Bad_Id.wave.json": "{}",
    f"{BP}/features/huge.wave.json": "{}",
    "docs/blueprints/halo.md": (
        "# Halo blueprint\n\nIntro, H-J99 is not named here.\n\n## 4. Journeys\n\n"
        "### Tier-1 journeys\n\n- H-J5 drag a layer\n- H-J40 owned by no feature\n\n"
        "## 5. Contracts\n\nH-J77 is past the section.\n"
    ),
}
HUGE = f"{BP}/features/huge.wave.json"
RECORD = (FX / "layer-keys-RECORD.md").read_text()


def sha(p: str) -> str:
    return hashlib.sha1(p.encode()).hexdigest()


def contents(text: str, size: int | None = None) -> dict:
    return {"size": size or len(text), "content": base64.b64encode(text.encode()).decode()}


class Fake:
    def __init__(self, repo: Path):
        self.repo, self.calls, self.limited, self.stalls = repo, [], False, 1
        self.lead_extra: dict = {}  # more lead-row fields (a v3 lead's ``slices``)
        self.origin = "https://github.com/qbtrix/creative-apps.git"
        self.heads = {"layer-keys": "a1", "easing": "b1", "Bad.Ref": "c1"}
        self.records = {"layer-keys": contents(RECORD), "easing": contents("Stage: 1 plan\n")}
        self.issues = [{"number": 1036, "state": "OPEN", "labels": [{"name": "feature-lead"}]}]
        self.comments: dict[int, list] = {}  # gh issue view's comments, oldest first
        self.files, self.prs, self.more_runs = dict(FILES), [], []
        self.inflight = self.peak = 0
        self.limited_records = False
        self.tree()

    def tree(self):
        tree = [*self.files, *(FX / "dev-tree.txt").read_text().split()]
        size = {HUGE: MAX_MODEL_BYTES + 1}
        self.ls = "".join(f"100644 blob {sha(p)} {size.get(p, 10)}\t{p}\n" for p in tree)

    def lead(self, f, state, *, merged=False, run=None, labels=(), comments=()):
        """A Halo feature with a wave file, a lead branch and a RECORD; ``state``
        ``None`` leaves its issue off the feature-lead list. ``labels`` go on its
        issue beside ``feature-lead``, ``comments`` (bodies) under it."""
        n = 2000 + len(self.files)
        wave = {"title": f, "wave": "W2", "status": "planned", "feature_issue": f"#{n}"}
        self.files[f"{BP}/features/{f}.wave.json"] = json.dumps(wave)
        names = ["feature-lead", *labels]
        row = {"number": n, "state": state, "labels": [{"name": x} for x in names]}
        row["updatedAt"] = "2026-10-18T00:00:00Z"
        at = "2026-10-17T0{}:00:00Z".format
        self.comments[n] = [{"createdAt": at(i), "body": b} for i, b in enumerate(comments)]
        self.issues += [row] if state else []
        self.prs += [{"number": n + 5000, "body": f"Feature: halo/{f}\n"}] if merged else []
        lead = {"feature": f, "app": "halo", "prs": [], "stalls": 0}
        if run:
            self.more_runs.append(
                {"action_id": f"act-{f}", "status": run, "repo": str(self.repo), "lead": lead}
            )
        self.heads[f], self.records[f] = "h1", contents(f"Stage: 3 {f}\n")
        self.tree()
        return row

    def reads(self, since=0):
        """The lead branches whose RECORD was fetched since call ``since``."""
        api = [c[2] for c in self.calls[since:] if c[1:2] == ["api"] and "contents/" in c[2]]
        return sorted(r.split("ref=lead/")[1] for r in api)

    async def git(self, argv):
        self.calls.append(argv)
        if argv[0] == "cat-file":
            return 0, next(t for p, t in self.files.items() if sha(p) == argv[2]), ""
        drag, parent = (
            "origin/dev:UITests/HaloJourneyTests+Drag.swift",
            "origin/dev:UITests/P.swift",
        )
        grep = (
            f"{drag}:    /// H-J5: a viewer drag keys a position.\n"
            f"{drag}-    @MainActor func testDragLayerToKeyPosition() {{\n--\n"
            f"{drag}:/// H-J5's and H-J6's steps over one launched Halo.\n"
            f"{drag}-@MainActor private struct ViewerDragSteps {{\n--\n"
            f"{parent}:    /// H-J23: parent a layer.\n"
            f"{parent}-    func testParentLayer() throws {{\n"
            f'{parent}-        XCTAssert(true, "H-J40 is not a journey comment")\n'
        )
        if argv[0] == "grep" and argv[argv.index("-A") + 1 if "-A" in argv else 0] in ("0", "grep"):
            grep = "".join(x for x in grep.splitlines(True) if ".swift:" in x)  # no context
        return 0, {"rev-parse": "62a8577\n", "ls-tree": self.ls, "grep": grep}.get(argv[0], ""), ""

    async def gh(self, argv):
        self.calls.append(argv)
        if argv[0] == "git":
            return 0, self.origin + "\n", ""
        if self.limited:
            return 1, "", "HTTP 403: API rate limit exceeded"
        rows: object = []
        if argv[1:3] == ["issue", "list"] and "qbtrix/creative-apps" in argv:
            rows = list(self.issues)
            if "feature-lead" not in argv:  # the waits_on read sees every issue
                rows.append({"number": 333, "state": "OPEN"})
        elif argv[1:3] == ["issue", "list"]:
            rows = [{"number": 171, "state": "CLOSED"}]
        elif argv[1:3] == ["issue", "view"]:
            rows = {"comments": self.comments.get(int(argv[3]), [])}
        elif argv[1:3] == ["pr", "list"]:
            rows = self.prs
        elif argv[1:3] == ["run", "list"]:
            rows = [{"headSha": "62a8577", "conclusion": "success", "url": "u"}]
        elif argv[1] == "api" and "matching-refs" in argv[2]:
            rows = [
                {"ref": f"refs/heads/lead/{f}", "object": {"sha": s}} for f, s in self.heads.items()
            ]
        elif argv[1] == "api":  # a RECORD: yield so concurrent reads overlap
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            await asyncio.sleep(0)
            self.inflight -= 1
            if self.limited_records:
                return 1, "", "HTTP 403: API rate limit exceeded"
            rows = self.records[argv[2].split("ref=lead/")[1]]
        return 0, json.dumps(rows), ""

    async def runs(self, ws):
        lead = {"feature": "layer-keys", "app": "halo", "prs": [], "stalls": self.stalls}
        lead |= self.lead_extra
        row = {"action_id": "act-1", "status": "running", "repo": str(self.repo), "lead": lead}
        return [row, *self.more_runs]

    async def touched(self, ws, action_id):
        return ["UITests/HaloJourneyTests+Drag.swift"]


@pytest.fixture
def fake(monkeypatch, tmp_path):
    for name in ("_CACHE", "_LOCKS", "_BLOBS"):
        monkeypatch.setattr(abr, name, {})
    monkeypatch.setattr(abr, "_re_resolve_repo", lambda r: (tmp_path.resolve(), None))
    return Fake(tmp_path.resolve())


async def read(fake, t=0.0):
    io = {"git": fake.git, "gh": fake.gh, "runs": fake.runs, "touched": fake.touched}
    return await abr.read_blueprint("ws1", "creative-apps", **io, now=lambda: t)


def feature(graph, fid):
    return next(f for f in graph["features"] if f["id"] == fid)


async def test_first_read_derives_the_graph(fake):
    g = await read(fake)
    assert (g["slug"], g["sha"], g["stale"]) == ("qbtrix/creative-apps", "62a8577", False)
    lk, live = feature(g, "halo/layer-keys"), feature(g, "halo/live-keys")
    assert lk["status"] == "building" and lk["run"]["stage"].startswith("5 finish")
    assert {"ref": "qbtrix/creative-apps#777", "state": "unknown"} in lk["waits_on"]
    assert live["waits_on"][0] == {"ref": "qbtrix/creative-core#171", "state": "closed"}
    assert ("claimed_ahead", "feature:halo/live-keys") in [(x["kind"], x["ref"]) for x in g["gaps"]]
    lead = {"action_id": "act-1", "kind": "lead", "feature": "halo/layer-keys"}
    assert g["in_flight"] == [lead | {"layers": ["tests"]}]
    assert g["components"] and {c["status"] for c in g["components"]} <= {
        "built",
        "partial",
        "planned",
    }
    assert g["apps"][0]["counts"] | {"building": 1, "gap": 1, "total": 2} == g["apps"][0]["counts"]
    assert g["dev_ci"] == {"sha": "62a8577", "state": "green", "url": "u"}


async def test_second_read_within_ttl_makes_no_calls(fake):
    first = await read(fake)
    n = len(fake.calls)
    assert await read(fake, t=59.0) is first and len(fake.calls) == n
    await read(fake, t=61.0)
    assert len(fake.calls) > n


async def test_moved_lead_head_rereads_only_that_record(fake):
    await read(fake)
    fake.heads["layer-keys"] = "a2"
    fake.records["layer-keys"] = contents("Stage: 6 landed\n")
    n = len(fake.calls)
    g = await read(fake, t=61.0)
    reads = [c[2] for c in fake.calls[n:] if c[1] == "api" and "contents/" in c[2]]
    assert [r.split("ref=")[1] for r in reads] == ["lead/layer-keys"]
    assert feature(g, "halo/layer-keys")["run"]["stage"] == "6 landed"


async def test_rate_limit_serves_the_last_graph_stale(fake):
    fake.limited = True
    with pytest.raises(BeltConsoleError) as err:  # nothing read yet
        await read(fake)
    assert err.value.status_code == 503
    fake.limited = False
    first = await read(fake, t=100.0)
    fake.limited = True
    g = await read(fake, t=161.0)
    assert g["stale"] is True and g["features"] == first["features"]


async def test_bad_id_and_oversized_files_are_refused_not_parsed(fake):
    g = await read(fake)
    cat = {c[2] for c in fake.calls if c[0] == "cat-file"}
    assert sha(f"{BP}/features/Bad_Id.wave.json") not in cat and sha(HUGE) not in cat
    assert {f["id"] for f in g["features"]} == {"halo/layer-keys", "halo/live-keys"}
    assert not any("Bad.Ref" in c[2] for c in fake.calls if c[1:2] == ["api"])
    entry = next(iter(abr._CACHE.values()))  # a bad ref never reaches a later read either
    assert "Bad.Ref" not in entry["heads"] and "Bad.Ref" not in entry["records"]
    fake.heads["layer-keys"] = "a3"
    fake.records["layer-keys"] = contents(RECORD, size=MAX_MODEL_BYTES + 1)
    g = await read(fake, t=61.0)
    assert feature(g, "halo/layer-keys")["run"]["stage"] is None


async def test_repo_outside_the_allowlist_is_404(fake, monkeypatch):
    monkeypatch.setattr(abr, "_re_resolve_repo", lambda r: (None, "not allowed"))
    with pytest.raises(BeltConsoleError) as err:
        await read(fake)
    assert err.value.status_code == 404 and fake.calls == []


async def test_two_stalls_on_the_real_int_shape_is_stuck(fake):
    assert feature(await read(fake), "halo/layer-keys")["status"] == "building"  # stalls=1
    fake.stalls = 2
    assert feature(await read(fake, t=61.0), "halo/layer-keys")["status"] == "stuck"


async def test_an_unexpected_error_serves_stale_or_503_never_a_bare_500(fake):
    good = fake.runs

    async def broken(ws):
        raise TypeError("a row of an unexpected shape")

    fake.runs = broken
    with pytest.raises(BeltConsoleError) as err:
        await read(fake)
    assert err.value.status_code == 503
    fake.runs = good
    first = await read(fake, t=100.0)
    fake.runs = broken
    g = await read(fake, t=161.0)
    assert g["stale"] is True and g["features"] == first["features"]


async def test_issues_come_from_the_feature_lead_label_and_always_carry_state(fake):
    g = await read(fake)
    lists = [c for c in fake.calls if c[1:3] == ["issue", "list"]]
    fields = "number,state,labels,updatedAt"
    mine = [c for c in lists if "qbtrix/creative-apps" in c and fields in c]
    assert mine and all(c[c.index("--label") + 1] == "feature-lead" for c in mine)
    assert not any("feature-lead" in c for c in lists if "qbtrix/creative-core" in c)
    lk, live = feature(g, "halo/layer-keys"), feature(g, "halo/live-keys")
    assert lk["issue"] == {"number": 1036, "state": "open", "labels": ["feature-lead"]}
    assert live["issue"] == {"number": 799, "state": "unknown", "labels": []}
    # a same-repo waits_on ref that is not a feature lead still resolves
    assert {"ref": "qbtrix/creative-apps#333", "state": "open"} in lk["waits_on"]
    assert lk["run"]["record_at"] is None


async def test_a_repo_not_on_github_has_an_empty_slug(fake):
    fake.origin = "git@example.com:acme/creative-apps.git"
    g = await read(fake)
    assert g["slug"] == "" and g["features"]


async def test_app_title_is_the_blueprint_heading_else_the_id(fake):
    assert (await read(fake))["apps"][0]["title"] == "Halo"
    fake.ls = "".join(x for x in fake.ls.splitlines(True) if "halo.md" not in x)
    assert (await read(fake, t=61.0))["apps"][0]["title"] == "halo"


async def test_journeys_are_named_from_the_note_and_the_journeys_section(fake):
    g = await read(fake)
    assert feature(g, "halo/layer-keys")["journeys"] == ["halo/H-J5", "halo/H-J6", "halo/H-J23"]
    rows = {j["id"]: j for j in g["journeys"]}
    assert set(rows) == {"halo/H-J5", "halo/H-J40", "halo/H-J6", "halo/H-J23"}
    assert rows["halo/H-J5"] | {"features": None} == {
        "id": "halo/H-J5",
        "test": "testDragLayerToKeyPosition",
        "file": "UITests/HaloJourneyTests+Drag.swift",
        "state": "existing",
        "features": None,
    }
    assert rows["halo/H-J6"]["test"] is None and rows["halo/H-J6"]["state"] == "existing"
    assert (rows["halo/H-J23"]["test"], rows["halo/H-J23"]["file"]) == (
        "testParentLayer",
        "UITests/P.swift",
    )
    assert rows["halo/H-J40"] == {
        "id": "halo/H-J40",
        "test": None,
        "file": None,
        "state": "named",
        "features": [],
    }
    assert g["apps"][0]["journeys"] == {"named": 4, "existing": 3, "passing": 0}


async def test_every_gap_has_text_and_unowned_planned_components_are_gaps(fake):
    g = await read(fake)
    assert g["gaps"] and all(isinstance(x["text"], str) and x["text"] for x in g["gaps"])
    ahead = next(x for x in g["gaps"] if x["kind"] == "claimed_ahead")
    assert "feature-done" in ahead["text"]
    comps = {c["id"]: c for c in g["components"]}
    unowned = [
        x["ref"].removeprefix("component:") for x in g["gaps"] if x["kind"] == "component_unowned"
    ]
    assert unowned and all(comps[c]["features"] == [] for c in unowned)
    assert all(comps[c]["status"] != "built" for c in unowned)
    assert {
        c for c, row in comps.items() if not row["features"] and row["status"] != "built"
    } == set(unowned)


async def test_features_carry_their_slices_and_apps_count_them(fake):
    path = f"{BP}/features/layer-keys.wave.json"
    wave = json.loads(FILES[path]) | {"slices": [{"id": "LK-a"}, {"id": "LK-b"}, {"id": "LK-c"}]}
    fake.files[path] = json.dumps(wave)
    fake.lead_extra = {
        "slices": [
            {"id": "LK-a", "done": True, "run": "r1", "gaps": [5]},
            {"id": "LK-b", "done": True, "run": "r2", "gaps": []},
        ]
    }
    g = await read(fake)
    got = [(s["id"], s["status"]) for s in feature(g, "halo/layer-keys")["slices"]]
    assert got == [("LK-a", "done-with-gaps"), ("LK-b", "done"), ("LK-c", "planned")]
    assert feature(g, "halo/live-keys")["slices"] == []
    assert g["apps"][0]["slices"] == {"done": 1, "done-with-gaps": 1, "building": 0, "planned": 1}


async def test_a_cold_read_fetches_only_records_that_can_still_change(fake):
    for i in range(30):  # closed and done: their RECORD changes nothing
        fake.lead(f"done-{i}", "CLOSED", merged=True)
    fake.lead("done-ran", "CLOSED", merged=True, run="landed")
    fake.lead("open", "OPEN", merged=True)  # its issue is open
    fake.lead("unmerged", "CLOSED")  # no feature-done PR has merged
    fake.lead("running", "CLOSED", merged=True, run="running")  # a pending run
    fake.lead("unlisted", None, merged=True)  # gh did not list its issue
    g = await read(fake)
    # easing has a lead branch but no wave file; Bad.Ref fails the id check
    assert fake.reads() == ["layer-keys", "open", "running", "unlisted", "unmerged"]
    assert feature(g, "halo/done-0")["status"] == "done"
    assert feature(g, "halo/done-ran")["run"]["stage"] is None
    assert feature(g, "halo/open")["status"] == "built"


async def test_record_reads_run_concurrently_within_the_bound(fake):
    for i in range(20):
        fake.lead(f"open-{i}", "OPEN")
    await read(fake)
    assert len(fake.reads()) == 21 and 1 < fake.peak <= 8
    assert fake.inflight == 0


async def test_a_done_lead_keeps_its_last_record_and_a_reopened_one_is_reread(fake):
    issue = fake.lead("shipped", "OPEN", run="landed")
    assert feature(await read(fake), "halo/shipped")["run"]["stage"] == "3 shipped"
    issue["state"] = "CLOSED"
    fake.prs.append({"number": 9001, "body": "Feature: halo/shipped"})
    fake.heads["shipped"], fake.records["shipped"] = "h2", contents("Stage: 9 later\n")
    n = len(fake.calls)
    g = await read(fake, t=61.0)
    assert fake.reads(n) == [] and feature(g, "halo/shipped")["run"]["stage"] == "3 shipped"
    issue["state"] = "OPEN"  # reopened: its head moved since the last read
    n = len(fake.calls)
    g = await read(fake, t=122.0)
    assert fake.reads(n) == ["shipped"] and feature(g, "halo/shipped")["run"]["stage"] == "9 later"


async def test_a_rate_limited_record_read_serves_stale_without_an_error_log(fake, caplog):
    for i in range(10):
        fake.lead(f"open-{i}", "OPEN")
    await read(fake)
    fake.heads = {f: "moved" for f in fake.heads}
    fake.limited_records = True
    with caplog.at_level(logging.ERROR, logger=abr.__name__):
        g = await read(fake, t=61.0)
    assert g["stale"] is True and not caplog.records


MAC = "Mac-Finish-Wanted: HaloTextJourneyTests is red since #2135, and it is macOS-only."
SHEET = "Belt-Contact-Sheet: https://github.com/qbtrix/creative-apps/actions/runs/39076534514"
ASK = "Which base should SE-b build on, dev or the design-seams branch?"


def views(fake, since=0):
    """The issue numbers whose comments were read since call ``since``."""
    return sorted(int(c[3]) for c in fake.calls[since:] if c[1:3] == ["issue", "view"])


async def test_issue_comments_split_parked_on_the_mac_released_done_and_stuck(fake):
    parked = fake.lead("parked", "OPEN", run="rejected", labels=["needs-human"], comments=[MAC])
    released = fake.lead("released", "OPEN", run="rejected", comments=[MAC, SHEET])
    asking = fake.lead("asking", "OPEN", run="rejected", labels=["needs-human"], comments=[ASK])
    # a sheet, then new Mac-only work: the newest of the two lines decides
    relapsed = fake.lead("relapsed", "OPEN", labels=["needs-human"], comments=[SHEET, MAC])
    fake.lead("live", "OPEN", run="running", comments=[SHEET])  # neither parked nor released
    g = await read(fake)
    names = ("parked", "released", "asking", "relapsed", "live")
    got = {f: feature(g, f"halo/{f}")["status"] for f in names}
    assert got == {
        "parked": "waiting",
        "released": "done",
        "asking": "stuck",
        "relapsed": "waiting",
        "live": "building",
    }
    assert g["apps"][0]["counts"]["waiting"] == 2
    nums = [parked["number"], released["number"], asking["number"], relapsed["number"]]
    assert views(fake) == sorted(nums)  # only an issue a comment can change is read


async def test_issue_comments_are_reread_only_when_the_issue_moved(fake):
    row = fake.lead("parked", "OPEN", run="rejected", labels=["needs-human"], comments=[ASK])
    assert feature(await read(fake), "halo/parked")["status"] == "stuck"
    n = len(fake.calls)
    await read(fake, t=61.0)
    assert views(fake, n) == []  # same updatedAt: the cached mark stands
    fake.comments[row["number"]].append({"createdAt": "2026-10-18T01:00:00Z", "body": MAC})
    row["updatedAt"] = "2026-10-18T01:00:00Z"
    n = len(fake.calls)
    g = await read(fake, t=122.0)
    assert views(fake, n) == [row["number"]] and feature(g, "halo/parked")["status"] == "waiting"


async def test_c4_paints_a_waiting_feature_amber_not_failed(fake):
    fake.issues[0]["labels"].append({"name": "needs-human"})
    fake.comments[1036] = [{"createdAt": "2026-10-17T23:07:08Z", "body": MAC}]
    io = {"git": fake.git, "gh": fake.gh, "runs": fake.runs, "touched": fake.touched}
    c4 = await abr.read_c4("ws1", "creative-apps", "halo", **io, now=lambda: 0.0)
    assert c4["status"]["halo-window"] == "drift"  # owned by the waiting halo/layer-keys


MAP = "docs/blueprints/suite/feature-map.json"


def suite_map(packets):
    rows = [
        {"id": f"S{i}", "name": n, "layer": "kernel", "packet": p}
        for i, (n, p) in enumerate(packets)
    ]
    layers = [{"id": "kernel", "name": "SuiteKernel"}]
    return json.dumps({"version": 1, "layers": layers, "apps": [], "features": rows})


async def read_map(fake, t=0.0):
    io = {"git": fake.git, "gh": fake.gh, "runs": fake.runs, "touched": fake.touched}
    return await abr.read_suite_map("ws1", "creative-apps", **io, now=lambda: t)


async def test_suite_map_joins_the_map_to_the_graph_status(fake):
    fake.files[MAP] = suite_map(
        [
            ("keys", "halo/layer-keys"),
            ("live", "halo/live-keys"),
            ("soon", "halo/next"),
            ("new", None),
        ]
    )
    fake.tree()
    m = await read_map(fake)
    assert (m["slug"], m["sha"], m["stale"]) == ("qbtrix/creative-apps", "62a8577", False)
    assert [f["status"] for f in m["features"]] == ["building", "gap", "writing", "new"]
    assert m["counts"]["building"] == 1 and m["counts"]["new"] == 1


async def test_suite_map_is_read_with_the_blueprint_off_one_cache_entry(fake):
    fake.files[MAP] = suite_map([("keys", "halo/layer-keys")])
    fake.tree()
    await read(fake)
    n = len(fake.calls)
    await read_map(fake, t=1.0)
    assert fake.calls[n:] == []  # inside the TTL: no git or gh call
    fake.limited = True
    m = await read_map(fake, t=61.0)  # a failed refresh: the last map, stale
    assert m["stale"] is True and m["features"][0]["status"] == "building"


async def test_suite_map_missing_or_unreadable_is_404_and_the_graph_still_reads(fake):
    for text in (None, "{not json", json.dumps({"features": "x"})):
        for name in ("_CACHE", "_LOCKS"):
            setattr(abr, name, {})
        fake.files.pop(MAP, None)
        if text is not None:
            fake.files[MAP] = text
        fake.tree()
        assert (await read(fake))["features"]  # the blueprint read never fails on the map
        with pytest.raises(BeltConsoleError) as err:
            await read_map(fake)
        assert (err.value.status_code, err.value.code) == (404, "belt.suite_map_not_found"), text
