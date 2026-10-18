# tests/cloud/test_belt_github_issues_patrol.py — the ``issues`` patrol on GitHub.
#
# A mandate whose bound repo has a GitHub origin reads its open issues through
# one ``gh issue list --label`` per charter ``issue_labels`` label and files one
# sighting per issue carrying one of them. ``gh`` (and the ``git remote get-url`` that
# finds the slug) is a fake executor here; nothing touches the network.
#
# Pinned: label matching (case-insensitive, objects from ``--json labels``), an
# old labelled issue past a busy repo's unfiltered list limit is still sighted
# with no ``gh issue view`` (``LimitedGh``), one failed label list files and
# resolves nothing, the label lists warm the dependency check's cache,
# severity (p1 and bug 4, p2 3), an empty ``issue_labels`` takes nothing, a
# GitLab-bound repo keeps the connector path, an unsafe slug never reaches gh, a
# gh failure files nothing; through mongomock, ``run_patrols`` dedupes on
# ``github:<owner>/<repo>#<n>`` across patrols (a hand-filed feedback sighting
# with that source blocks the patrol's copy), the continuous due check runs
# the issues patrol first, only on mandates that toggle it, and the foreman's
# prompt carries a sighting's ``Depends on:`` line (bold or plain) past the
# summary cut, with each named issue's state from one ``gh issue list --state all``.
# A sighting whose issue an OPEN PR closes (one ``gh pr list --state open``) is
# in flight: the due check skips it, the foreman sees ``open PR #n`` and the
# digest agrees; a PR closing #120 is not one for #12, and a gh failure keeps
# today's behaviour.

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("mongomock_motor")

pytestmark = pytest.mark.usefixtures("any_repo_root")

from pocketpaw_ee.cloud.mandates import patrols  # noqa: E402
from pocketpaw_ee.cloud.mandates.patrols import issues_patrol  # noqa: E402

from tests.cloud.test_belt_mandates import graph, journal, store  # noqa: E402,F401

SLUG = "qbtrix/creative-core"


def _issue(n: int, *labels: str, title: str = "") -> dict:
    return {
        "number": n,
        "title": title or f"Issue {n}",
        "body": f"**Behavior**\nthing {n}\n\n**Acceptance test**\nit works",
        "labels": [{"name": name, "color": "ffffff", "description": ""} for name in labels],
        "url": f"https://github.com/{SLUG}/issues/{n}",
    }


class FakeGh:
    """Answers ``git remote get-url`` with ``origin``, ``gh issue list --state
    all`` with ``states`` (``{number: "OPEN"|"CLOSED"}``), any other ``gh
    issue list`` with ``issues``, ``gh pr list`` with ``prs`` (exit
    ``pr_code``) and ``gh issue view <n> -R <slug>`` with ``views[n]`` or
    ``views["<slug>#<n>"]`` (exit 1 when absent); records every argv."""

    def __init__(
        self,
        issues: list[dict],
        *,
        origin: str = SLUG,
        code: int = 0,
        states: dict[int, str] | None = None,
        prs: list[dict] | None = None,
        pr_code: int = 0,
        views: dict | None = None,
    ) -> None:
        self.issues = issues
        self.views = views or {}
        self.prs = prs or []
        self.pr_code = pr_code
        self.origin = origin
        self.code = code
        self.states = states or {}
        self.calls: list[list[str]] = []

    async def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        if "get-url" in argv:
            return 0, f"https://github.com/{self.origin}.git\n", ""
        if argv[:3] == ["gh", "issue", "list"] and "all" in argv:
            return 0, json.dumps([{"number": n, "state": s} for n, s in self.states.items()]), ""
        if argv[:3] == ["gh", "issue", "list"]:
            return self.code, json.dumps(self.issues), "HTTP 502" if self.code else ""
        if argv[:3] == ["gh", "pr", "list"]:
            return self.pr_code, json.dumps(self.prs), "HTTP 502" if self.pr_code else ""
        if argv[:3] == ["gh", "issue", "view"]:
            n, slug = argv[3], argv[5]
            view = self.views.get(f"{slug}#{n}", self.views.get(int(n)))
            return (0, json.dumps(view), "") if view is not None else (1, "", "not found")
        raise AssertionError(f"unexpected argv {argv}")


class LimitedGh(FakeGh):
    """``FakeGh`` whose unfiltered open issue list stops short of ``hidden`` (an
    old issue past ``--limit`` on a busy repo), while a ``--label`` list returns
    every issue carrying that label (exit 1 for a label in ``failing``)."""

    hidden: set[int] = set()
    failing: set[str] = set()

    async def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        if argv[:3] == ["gh", "issue", "list"] and "all" not in argv:
            self.calls.append(list(argv))
            if "--label" in argv:
                want = argv[argv.index("--label") + 1]
                if want in self.failing:
                    return 1, "", "HTTP 502"
                rows = [i for i in self.issues if want in {lb["name"] for lb in i["labels"]}]
            else:
                rows = [i for i in self.issues if i["number"] not in self.hidden]
            return 0, json.dumps(rows), ""
        return await super().__call__(argv)


@pytest.fixture(autouse=True)
def _fresh_gh_caches(monkeypatch) -> None:
    """The gh reads are cached per repo for a scheduler tick, across tests too."""
    monkeypatch.setattr(patrols, "_ISSUE_STATES", {})
    monkeypatch.setattr(patrols, "_OPEN_PRS", {})
    monkeypatch.setattr(patrols, "_OPEN_ISSUES", {})
    monkeypatch.setattr(patrols, "_ISSUE_VIEWS", {})
    monkeypatch.setattr(patrols, "_LABELLED_BODIES", {})


def _issue_lists(gh: FakeGh) -> list[list[str]]:
    return [a for a in gh.calls if a[:3] == ["gh", "issue", "list"]]


async def _patrol(gh: FakeGh, labels: list[str], tmp_path: Path) -> list[dict]:
    return await issues_patrol(str(tmp_path), workspace_id="w1", issue_labels=labels, gh=gh)


async def test_labelled_open_issues_become_sightings(tmp_path):
    gh = FakeGh(
        [
            _issue(115, "suite", "p2"),
            _issue(116, "needs-core", "bug"),
            _issue(117, "line:flare", "p1"),  # another line's issue
            _issue(118, "Suite", "p1"),  # labels match case-insensitively
            _issue(119, "suite"),
        ]
    )
    drafts = await _patrol(gh, ["suite", "needs-core"], tmp_path)

    by_n = {d["evidence"]["number"]: d for d in drafts}
    assert sorted(by_n) == [115, 116, 118, 119]
    assert {n: d["severity"] for n, d in by_n.items()} == {115: 3, 116: 4, 118: 4, 119: 2}
    d = by_n[115]
    assert d["patrol"] == "issues"
    assert d["evidence"]["source"] == f"github:{SLUG}#115"
    assert d["evidence"]["dedup_key"] == f"github:{SLUG}#115"
    assert d["evidence"]["labels"] == ["suite", "p2"]
    assert d["evidence"]["url"] == f"https://github.com/{SLUG}/issues/115"
    assert d["summary"].startswith("GitHub issue #115: Issue 115")
    lists = _issue_lists(gh)
    assert [a[a.index("--label") + 1] for a in lists] == ["suite", "needs-core"]
    assert all(a[:5] == ["gh", "issue", "list", "-R", SLUG] and "open" in a for a in lists)


async def test_no_issue_labels_takes_nothing(tmp_path):
    """No labels lists nothing and resolves nothing: a cleared charter must not
    read every sighted issue as gone from the line."""
    gh = FakeGh([_issue(115, "suite")], views={115: {"state": "OPEN", "labels": []}})
    drafts = await issues_patrol(
        str(tmp_path), workspace_id="w1", issue_labels=[], gh=gh, sighted=[f"github:{SLUG}#115"]
    )
    assert drafts == []
    assert [a for a in gh.calls if a[:2] == ["gh", "issue"]] == []


async def test_gitlab_bound_repo_keeps_the_connector_path(tmp_path):
    gh = FakeGh([], origin="unused")

    async def git_url(argv):
        gh.calls.append(list(argv))
        return 0, "https://gitlab.com/acme/app.git\n", ""

    calls: list[tuple] = []

    async def execute(workspace_id, name, body, *, user_id=None):
        calls.append((workspace_id, name, body))
        return SimpleNamespace(success=True, data=[{"iid": 3, "title": "Bug", "state": "opened"}])

    drafts = await issues_patrol(
        str(tmp_path), workspace_id="w1", issue_labels=["suite"], gh=git_url, execute=execute
    )
    assert [d["evidence"]["iid"] for d in drafts] == [3]
    assert calls and calls[0][1] == "gitlab"
    assert _issue_lists(gh) == []


@pytest.mark.parametrize("origin", ["../evil", "-x/repo", "owner/.."])
async def test_unsafe_slug_never_reaches_gh(tmp_path, origin):
    gh = FakeGh([_issue(115, "suite")], origin=origin)
    assert await _patrol(gh, ["suite"], tmp_path) == []
    assert _issue_lists(gh) == []


async def test_gh_failure_files_nothing(tmp_path):
    gh = FakeGh([_issue(115, "suite")], code=1)
    assert await _patrol(gh, ["suite"], tmp_path) == []


def test_dto_takes_issue_labels():
    from pocketpaw_ee.cloud.mandates.dto import CharterRequest

    charter = CharterRequest.model_validate({"goal": "g", "issue_labels": ["line:flare"]})
    assert charter.issue_labels == ["line:flare"]
    assert CharterRequest.model_validate({"goal": "g"}).issue_labels == []


async def _github_mandate(tmp_path: Path, monkeypatch, gh: FakeGh, **charter) -> str:
    from pocketpaw_ee.cloud.mandates import service as mandate_service

    async def patrol(repo_id, *, workspace_id, user_id=None, issue_labels=None, sighted=None):
        return await issues_patrol(
            repo_id, workspace_id=workspace_id, issue_labels=issue_labels, gh=gh, sighted=sighted
        )

    monkeypatch.setitem(patrols.PATROLS, "issues", patrol)
    created = await mandate_service.create_mandate(
        "w1",
        "u1",
        {
            "name": "engines",
            "surface": {"repo_id": str(tmp_path)},
            "charter": {"goal": "ship the engines", "issue_labels": ["suite"], **charter},
            "patrols": ["issues", "feedback"],
        },
    )
    return created["mandate"]["id"]


async def test_run_patrols_dedupes_on_the_issue_across_patrols(tmp_path, mongo_db, monkeypatch):
    from pocketpaw_ee.cloud.mandates import service as mandate_service

    gh = FakeGh([_issue(115, "suite"), _issue(116, "suite", "p1")])
    mandate_id = await _github_mandate(tmp_path, monkeypatch, gh)
    detail = await mandate_service.get_mandate("w1", "u1", mandate_id)
    assert detail["charter"]["issue_labels"] == ["suite"]
    # The pilot filed #115 by hand through the feedback intake.
    await mandate_service.file_feedback(
        "w1",
        "u1",
        mandate_id,
        {"text": "GitHub issue #115 ...", "severity": 3, "source": f"github:{SLUG}#115"},
    )

    first = (await mandate_service.run_patrols("w1", "u1", mandate_id))["sightings"]
    assert [s["evidence"]["number"] for s in first] == [116]
    assert first[0]["severity"] == 4

    again = await mandate_service.run_patrols("w1", "u1", mandate_id)
    assert again["sightings"] == []


async def test_continuous_due_check_runs_the_issues_patrol_first(tmp_path, mongo_db, monkeypatch):
    from pocketpaw_ee.cloud.mandates import service as mandate_service

    gh = FakeGh([_issue(120, "suite", "p2")])
    mandate_id = await _github_mandate(tmp_path, monkeypatch, gh, cadence="continuous")

    rows = await mandate_service.list_cadence_due(datetime.now(UTC))

    assert mandate_id in {r["mandate_id"] for r in rows}
    sightings = (await mandate_service.list_sightings("w1", "u1", mandate_id))["sightings"]
    assert [s["evidence"]["number"] for s in sightings] == [120]


async def test_continuous_due_check_skips_a_mandate_without_the_issues_toggle(
    tmp_path, mongo_db, monkeypatch
):
    from pocketpaw_ee.cloud.mandates import service as mandate_service

    gh = FakeGh([_issue(120, "suite")])
    await _github_mandate(tmp_path, monkeypatch, gh, cadence="continuous")
    created = await mandate_service.create_mandate(
        "w1",
        "u1",
        {
            "name": "no issues toggle",
            "surface": {"repo_id": str(tmp_path)},
            "charter": {"goal": "g", "cadence": "continuous", "issue_labels": ["suite"]},
            "patrols": ["feedback"],
        },
    )
    await mandate_service.list_cadence_due(datetime.now(UTC))

    # Only the toggled mandate listed issues (once); the other never did.
    assert len(_issue_lists(gh)) == 1
    other = created["mandate"]["id"]
    assert (await mandate_service.list_sightings("w1", "u1", other))["sightings"] == []


async def test_a_busy_line_still_sights_new_issues(tmp_path, mongo_db, monkeypatch, store):  # noqa: F811
    """A line at its in-flight cap holds the next shift, but never the patrol: a
    tester's bug filed while the line is building must reach the backlog."""
    from datetime import timedelta

    from pocketpaw_ee.cloud.mandates import service as mandate_service
    from pocketpaw_ee.cloud.mandates.domain import ShiftDoc

    from tests.cloud.test_belt_mandates import _seed_run

    gh = FakeGh([_issue(121, "suite", "bug")])
    mandate_id = await _github_mandate(
        tmp_path, monkeypatch, gh, cadence="continuous", budget={"max_in_flight": 1}
    )
    await _seed_run(
        store, mandate_id, "in flight", plan_action_id="plan-1", task_index=1, station_pending=True
    )
    shift = ShiftDoc(
        workspace="w1", mandate_id=mandate_id, no=1, state="done", plan_action_id="plan-1"
    )
    await shift.insert()
    await shift.set({ShiftDoc.createdAt: datetime.now(UTC) - timedelta(minutes=30)})

    rows = await mandate_service.list_cadence_due(datetime.now(UTC))

    assert mandate_id not in {r["mandate_id"] for r in rows}, "the line is at its cap of 1"
    sightings = (await mandate_service.list_sightings("w1", "u1", mandate_id))["sightings"]
    assert [s["evidence"]["number"] for s in sightings] == [121]


@pytest.mark.parametrize(
    "line",
    ["**Depends on:** #4, #5", "**Depends on**: #4, #5", "Depends on: #4, #5", "- depends on: #4"],
)
def test_a_depends_on_line_is_found_bold_or_plain(line):
    from pocketpaw_ee.cloud.mandates.service import _depends_on

    assert _depends_on(f"**Behavior**\nthis depends on: nothing\n\n{line}\n") == line
    assert _depends_on("no dependency here") is None


async def test_the_foreman_sees_a_cut_depends_on_line_and_the_issue_states(
    tmp_path,
    mongo_db,
    monkeypatch,
    store,  # noqa: F811
    journal,  # noqa: F811
    graph,  # noqa: F811
):
    """The summary is cut at 280 characters and the ``Depends on:`` line ends a
    long body: the foreman still reads it, with each named issue's state, so it
    can plan work whose dependencies are closed."""
    from pocketpaw_ee.cloud.mandates import foreman
    from pocketpaw_ee.cloud.mandates import service as mandate_service

    issue = _issue(12, "suite")
    filler = "the export panel drops frames " * 30
    issue["body"] = f"**Behavior**\n{filler}\n\nDepends on: #4, #5"
    gh = FakeGh([issue], states={4: "CLOSED", 5: "CLOSED", 12: "OPEN"})
    monkeypatch.setattr(patrols, "_default_gh", gh)
    monkeypatch.setattr(patrols, "_ISSUE_STATES", {})
    monkeypatch.setenv("POCKETPAW_MANDATE_LLM", "mock")
    prompts: list[str] = []
    real_plan = foreman.MockLlm.plan

    async def capture(self, *, prompt, context):
        prompts.append(prompt)
        return await real_plan(self, prompt=prompt, context=context)

    monkeypatch.setattr(foreman.MockLlm, "plan", capture)
    mandate_id = await _github_mandate(tmp_path, monkeypatch, gh)

    await mandate_service.trigger_shift("w1", "u1", mandate_id)

    (prompt,) = prompts
    (sighting,) = (await mandate_service.list_sightings("w1", "u1", mandate_id))["sightings"]
    assert "Depends on" not in sighting["summary"], "the summary cut drops the line"
    assert "Depends on: #4, #5" in prompt
    assert "#4 closed, #5 closed" in prompt
    assert len([a for a in gh.calls if a[:3] == ["gh", "issue", "list"] and "all" in a]) == 1


@pytest.mark.parametrize(
    ("prs", "pr_code", "due"),
    [
        ([{"number": 160, "body": "Adds text shapes.\n\nfixes #12"}], 0, False),
        ([{"number": 161, "body": "RESOLVES #12"}], 0, False),
        ([{"number": 162, "body": "Closes #120\nRefs #12"}], 0, True),
        ([{"number": 160, "body": "Closes #12"}], 1, True),  # gh failed: as before
    ],
)
async def test_an_open_pr_that_closes_the_issue_keeps_its_sighting_in_flight(
    tmp_path, mongo_db, monkeypatch, prs, pr_code, due
):
    """Another run's PR still waiting in the fast lane works the issue: a new
    shift must not plan it again, whatever happened to that run."""
    from pocketpaw_ee.cloud.mandates import service as mandate_service

    gh = FakeGh([_issue(12, "suite")], prs=prs, pr_code=pr_code)
    monkeypatch.setattr(patrols, "_default_gh", gh)
    mandate_id = await _github_mandate(tmp_path, monkeypatch, gh, cadence="continuous")

    rows = await mandate_service.list_cadence_due(datetime.now(UTC))

    assert (mandate_id in {r["mandate_id"] for r in rows}) is due
    (listed,) = [a for a in gh.calls if a[:3] == ["gh", "pr", "list"]]
    assert listed[3:7] == ["-R", SLUG, "--state", "open"] and "--limit" in listed


async def test_the_foreman_does_not_plan_an_issue_an_open_pr_closes(
    tmp_path,
    mongo_db,
    monkeypatch,
    store,  # noqa: F811
    journal,  # noqa: F811
    graph,  # noqa: F811
):
    from pocketpaw_ee.cloud.mandates import foreman
    from pocketpaw_ee.cloud.mandates import service as mandate_service

    gh = FakeGh([_issue(12, "suite")], prs=[{"number": 160, "body": "Fixes #12"}])
    monkeypatch.setattr(patrols, "_default_gh", gh)
    monkeypatch.setenv("POCKETPAW_MANDATE_LLM", "mock")
    prompts: list[str] = []
    real_plan = foreman.MockLlm.plan

    async def capture(self, *, prompt, context):
        prompts.append(prompt)
        return await real_plan(self, prompt=prompt, context=context)

    monkeypatch.setattr(foreman.MockLlm, "plan", capture)
    mandate_id = await _github_mandate(tmp_path, monkeypatch, gh)

    out = await mandate_service.trigger_shift("w1", "u1", mandate_id)

    assert out["shift"]["state"] == "stood_down"
    (prompt,) = prompts
    assert "IN FLIGHT | open PR #160]: GitHub issue #12" in prompt
    # The morning digest agrees with what the foreman saw.
    (row,) = (await mandate_service.digest("w1", "u1"))["mandates"]
    assert [b["in_flight"] for b in row["backlog"]["top"]] == [True]


# A busy repo (hundreds of open issues) cuts the unfiltered open list short:
# the patrol lists each charter label on its own so an old labelled issue is
# still sighted, and the line's sighted issues need no ``gh issue view``.


def _busy_repo() -> LimitedGh:
    gh = LimitedGh(
        [_issue(5, "suite", "p1"), _issue(6, "suite", "needs-core"), _issue(7, "other")]
        + [_issue(n, "noise") for n in range(200, 210)]
    )
    gh.hidden = {5, 6, 7}
    return gh


async def test_a_labelled_issue_past_the_open_list_limit_is_sighted(tmp_path):
    """Mutation: reading only the unfiltered list never sights #5 or #6."""
    gh = _busy_repo()
    drafts = await _patrol(gh, ["suite", "needs-core"], tmp_path)

    assert [d["evidence"]["number"] for d in drafts] == [5, 6], "#6 carries both labels once"
    assert all("--label" in a for a in _issue_lists(gh)), "no unfiltered list"


async def test_a_sighted_issue_past_the_limit_keeps_its_line_without_a_view(tmp_path):
    """Mutation: departures read off the unfiltered list view #5 every pass."""
    gh = _busy_repo()
    drafts = await issues_patrol(
        str(tmp_path),
        workspace_id="w1",
        issue_labels=["suite"],
        gh=gh,
        sighted=[f"github:{SLUG}#5", f"github:{SLUG}#6"],
    )

    assert [d.get("resolve") for d in drafts] == [None, None]
    assert [a for a in gh.calls if a[:3] == ["gh", "issue", "view"]] == []


async def test_one_failed_label_list_files_and_resolves_nothing(tmp_path):
    """A label list that fails would make its issues look like they left the
    line: the pass files nothing, resolves nothing and views nothing."""
    gh = _busy_repo()
    gh.failing = {"needs-core"}
    gh.views = {5: {"state": "CLOSED", "labels": [], "body": ""}}
    drafts = await issues_patrol(
        str(tmp_path),
        workspace_id="w1",
        issue_labels=["suite", "needs-core"],
        gh=gh,
        sighted=[f"github:{SLUG}#5", f"github:{SLUG}#6"],
    )

    assert drafts == []
    assert [a for a in gh.calls if a[:3] == ["gh", "issue", "view"]] == []


async def test_the_patrol_label_lists_serve_the_dependency_check(tmp_path):
    """The patrol and the backlog's dependency check run in one tick: each
    charter label is listed once between them (GraphQL budget). Mutation: the
    patrol not warming the cache lists ``suite`` twice."""
    gh = _busy_repo()
    await _patrol(gh, ["suite"], tmp_path)
    blocked = await patrols.open_dependencies(
        str(tmp_path), [f"github:{SLUG}#5"], gh=gh, labels=("suite",)
    )

    assert blocked == {}
    labelled = [a for a in _issue_lists(gh) if "--label" in a]
    assert len(labelled) == 1 and _issue_lists(gh)[0] == labelled[0], "the patrol's list"
