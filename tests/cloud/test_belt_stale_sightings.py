# tests/cloud/test_belt_stale_sightings.py — work the foreman must not plan.
#
# An issue sighting goes stale two ways. Its issue closes, or leaves the line
# (relabelled): the ``issues`` patrol then resolves it as
# ``patrol:issue-closed`` / ``patrol:issue-left-line``. A hand-filed sighting
# only resolves on close; a gh failure resolves nothing; a reopened issue is
# sighted again. Until the patrol runs, the backlog skips a closed issue. And an
# issue whose live body names a ``Depends on`` issue still open (``#N``,
# ``owner/repo#N``, a heading over a list, no colon) is blocked: neither the
# foreman nor the due check takes it, and a gh failure blocks too. A ref in a
# note after the refs (`` (`` or `` - ``) is not a dependency; ``none`` is none.
# Real store (mongomock), real trigger, mock foreman; gh is a fake executor.

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("mongomock_motor")

from pocketpaw_ee.cloud.mandates import patrols  # noqa: E402
from pocketpaw_ee.cloud.mandates import service as mandate_service  # noqa: E402
from pocketpaw_ee.cloud.mandates.domain import SightingDoc  # noqa: E402

from tests.cloud.test_belt_github_issues_patrol import (  # noqa: E402
    SLUG,
    FakeGh,
    _fresh_gh_caches,  # noqa: F401 — autouse: the gh caches are per test
    _github_mandate,
    _issue,
)
from tests.cloud.test_belt_mandates import foreman_calls, graph, journal, store  # noqa: E402,F401

pytestmark = pytest.mark.usefixtures("any_repo_root")


def _closed(*labels: str) -> dict:
    return {"state": "CLOSED", "labels": [{"name": n} for n in labels], "body": ""}


def _open(*labels: str, body: str = "") -> dict:
    return {"state": "OPEN", "labels": [{"name": n} for n in labels], "body": body}


async def _by_number(mandate_id: str) -> dict[int, list[SightingDoc]]:
    docs = (
        await SightingDoc.find(SightingDoc.workspace == "w1", SightingDoc.mandate_id == mandate_id)
        .sort("ts")
        .to_list()
    )
    out: dict[int, list[SightingDoc]] = {}
    for d in docs:
        out.setdefault(int(str(d.evidence["source"]).rsplit("#", 1)[1]), []).append(d)
    return out


async def _hand_file(mandate_id: str, n: int) -> None:
    await mandate_service.file_feedback(
        "w1",
        "u1",
        mandate_id,
        {"text": f"GitHub issue #{n}", "severity": 3, "source": f"github:{SLUG}#{n}"},
    )


def _next_tick() -> None:
    """The issue views are cached for a scheduler tick; a new tick reads fresh."""
    patrols._ISSUE_VIEWS.clear()


def _views(gh: FakeGh) -> list[list[str]]:
    return [a for a in gh.calls if a[:3] == ["gh", "issue", "view"]]


# ---------------------------------------------------------------------------
# The patrol resolves what closed or left the line
# ---------------------------------------------------------------------------


async def test_a_closed_issue_resolves_its_sighting(tmp_path, mongo_db, monkeypatch):
    gh = FakeGh([_issue(23, "suite"), _issue(24, "suite")], views={25: _open()})
    mandate_id = await _github_mandate(tmp_path, monkeypatch, gh)
    await _hand_file(mandate_id, 25)  # open, no line label: on the line by hand
    await mandate_service.run_patrols("w1", "u1", mandate_id)
    assert {n: s[0].resolved_by_run for n, s in (await _by_number(mandate_id)).items()} == {
        23: None,
        24: None,
        25: None,
    }

    gh.issues = [_issue(24, "suite")]
    gh.views = {23: _closed("suite"), 25: _closed()}
    _next_tick()
    out = await mandate_service.run_patrols("w1", "u1", mandate_id)

    assert out["sightings"] == []
    rows = await _by_number(mandate_id)
    assert {n: s[0].resolved_by_run for n, s in rows.items()} == {
        23: "patrol:issue-closed",
        24: None,
        25: "patrol:issue-closed",
    }
    assert rows[23][0].resolved_at is not None and rows[24][0].resolved_at is None
    last = _views(gh)[-2:]
    assert sorted(a[3] for a in last) == ["23", "25"]
    assert all(a[4:] == ["-R", SLUG, "--json", "state,labels,body"] for a in last)


async def test_an_issue_relabelled_off_the_line_resolves_with_its_own_reason(
    tmp_path, mongo_db, monkeypatch
):
    gh = FakeGh([_issue(n, "suite") for n in (24, 27, 28)] + [_issue(26, "other")])
    mandate_id = await _github_mandate(tmp_path, monkeypatch, gh)
    await _hand_file(mandate_id, 26)
    await mandate_service.run_patrols("w1", "u1", mandate_id)

    # #24 is still open, now on another line; #27 and #28 fell past the open
    # list's limit, so gh views them: #27 still carries the line's label.
    gh.issues = [_issue(24, "other"), _issue(26, "other")]
    gh.views = {27: _open("suite"), 28: _open("other")}
    _next_tick()
    await mandate_service.run_patrols("w1", "u1", mandate_id)

    rows = await _by_number(mandate_id)
    assert {n: s[0].resolved_by_run for n, s in rows.items()} == {
        24: "patrol:issue-left-line",
        26: None,  # hand-filed: only closing resolves it
        27: None,
        28: "patrol:issue-left-line",
    }
    assert sorted(a[3] for a in _views(gh)) == ["27", "28"], "an open-listed issue needs no view"


async def test_a_reopened_issue_is_sighted_again(tmp_path, mongo_db, monkeypatch):
    gh = FakeGh([_issue(23, "suite")])
    mandate_id = await _github_mandate(tmp_path, monkeypatch, gh)
    await mandate_service.run_patrols("w1", "u1", mandate_id)
    gh.issues, gh.views = [], {23: _closed("suite")}
    _next_tick()
    await mandate_service.run_patrols("w1", "u1", mandate_id)

    gh.issues = [_issue(23, "suite")]  # reopened
    _next_tick()
    again = (await mandate_service.run_patrols("w1", "u1", mandate_id))["sightings"]
    assert [s["evidence"]["number"] for s in again] == [23]
    assert (await mandate_service.run_patrols("w1", "u1", mandate_id))["sightings"] == []

    (first, second) = (await _by_number(mandate_id))[23]
    assert first.resolved_by_run == "patrol:issue-closed"
    assert second.resolved_by_run is None


@pytest.mark.parametrize(
    ("list_code", "views"),
    [(1, {23: _closed("suite")}), (0, {})],
    ids=["gh-issue-list-fails", "gh-issue-view-fails"],
)
async def test_a_gh_failure_resolves_nothing(tmp_path, mongo_db, monkeypatch, list_code, views):
    gh = FakeGh([_issue(23, "suite")])
    mandate_id = await _github_mandate(tmp_path, monkeypatch, gh)
    await _hand_file(mandate_id, 30)
    await mandate_service.run_patrols("w1", "u1", mandate_id)

    gh.issues, gh.code, gh.views = [], list_code, views
    _next_tick()
    before = len(_views(gh))
    await mandate_service.run_patrols("w1", "u1", mandate_id)

    rows = await _by_number(mandate_id)
    assert {n: s[0].resolved_by_run for n, s in rows.items()} == {23: None, 30: None}
    # A failed list is no evidence at all: nothing is even viewed.
    assert len(_views(gh)) - before == (0 if list_code else 2)


async def test_issue_views_are_capped_and_cached_per_tick(tmp_path):
    """A line with many stale sightings views at most ``_MAX_ISSUE_VIEWS`` a
    pass, and a second pass in the same tick reuses them."""
    gh = FakeGh([], views={n: _closed("suite") for n in range(1, 31)})
    sighted = [f"github:{SLUG}#{n}" for n in range(1, 31)]

    async def patrol() -> list[dict]:
        return await patrols.issues_patrol(
            str(tmp_path), workspace_id="w1", issue_labels=["suite"], gh=gh, sighted=sighted
        )

    first = await patrol()
    assert len(first) == len(_views(gh)) == patrols._MAX_ISSUE_VIEWS == 20
    assert {d["reason"] for d in first} == {"issue-closed"}
    second = await patrol()
    assert len(_views(gh)) == 30, "the second pass views only the ten it had not"
    assert len(second) == 30


# ---------------------------------------------------------------------------
# The backlog: a closed issue is no work, an open dependency blocks
# ---------------------------------------------------------------------------


async def _feedback_mandate(tmp_path) -> str:
    """A line without the ``issues`` patrol: nothing resolves its sightings
    before the backlog reads them."""
    created = await mandate_service.create_mandate(
        "w1",
        "u1",
        {
            "name": "photo",
            "surface": {"repo_id": str(tmp_path)},
            "charter": {"goal": "ship photo", "cadence": "continuous"},
            "patrols": ["feedback"],
        },
    )
    return created["mandate"]["id"]


@pytest.mark.usefixtures("store", "journal", "graph")
async def test_the_foreman_backlog_skips_closed_and_blocked_issues(
    tmp_path,
    mongo_db,
    monkeypatch,
    foreman_calls,  # noqa: F811
):
    monkeypatch.setenv("POCKETPAW_MANDATE_LLM", "mock")
    blocked = _issue(31, "suite")
    blocked["body"] = "**Behavior**\nexport\n\nDepends on #412"
    gh = FakeGh(
        [blocked, _issue(32, "suite")],
        states={30: "CLOSED", 31: "OPEN", 32: "OPEN", 412: "OPEN"},
        views={30: _closed("suite")},  # readable, so only the closed skip hides it
    )
    monkeypatch.setattr(patrols, "_default_gh", gh)
    mandate_id = await _feedback_mandate(tmp_path)
    for n in (30, 31, 32):
        await _hand_file(mandate_id, n)

    await mandate_service.trigger_shift("w1", "u1", mandate_id)

    ctx = foreman_calls[-1].context
    assert [s["summary"] for s in ctx.sightings] == ["GitHub issue #32"]
    assert ctx.open_total == 1
    assert "#30" not in foreman_calls[-1].prompt and "#31" not in foreman_calls[-1].prompt
    # Skipped, not resolved: a reopened issue comes back into the backlog.
    assert all(s[0].resolved_by_run is None for s in (await _by_number(mandate_id)).values())


async def test_a_line_whose_only_work_is_blocked_is_not_due(tmp_path, mongo_db, monkeypatch):
    issue = _issue(31, "suite")
    issue["body"] = "## Depends on\n\n- qbtrix/photo-core#230\n"
    gh = FakeGh([issue], states={31: "OPEN"}, views={"qbtrix/photo-core#230": _open()})
    monkeypatch.setattr(patrols, "_default_gh", gh)
    mandate_id = await _feedback_mandate(tmp_path)
    await _hand_file(mandate_id, 31)

    rows = await mandate_service.list_cadence_due(datetime.now(UTC))
    assert mandate_id not in {r["mandate_id"] for r in rows}

    gh.views = {"qbtrix/photo-core#230": _closed()}
    _next_tick()
    rows = await mandate_service.list_cadence_due(datetime.now(UTC))
    assert mandate_id in {r["mandate_id"] for r in rows}


# ---------------------------------------------------------------------------
# open_dependencies: every Depends-on form, read off the LIVE body
# ---------------------------------------------------------------------------


def _src(n: int) -> str:
    return f"github:{SLUG}#{n}"


@pytest.mark.parametrize(
    ("body", "blocked_by"),
    [
        ("**Depends on:** #4, #412", [f"{SLUG}#412"]),
        ("Body\n\nDepends on #412", [f"{SLUG}#412"]),  # no colon
        ("Depends on: qbtrix/photo-core#230 and #4", ["qbtrix/photo-core#230"]),
        (
            "## Depends on\n\n- #412\n- qbtrix/photo-core#230\n",
            [f"{SLUG}#412", "qbtrix/photo-core#230"],
        ),
        ("**Depends on**\n- #4\n**Acceptance test**\nsee #412", []),  # the section ends
        ("Depends on: https://github.com/qbtrix/photo-core/issues/230", ["qbtrix/photo-core#230"]),
        ("This depends on: #412 being fast\n\nnothing else", []),  # prose, not the line
        ("Depends on: #4", []),  # its dependency closed
    ],
    ids=[
        "colon",
        "no-colon",
        "cross-repo",
        "heading-list",
        "section-end",
        "url",
        "prose",
        "closed",
    ],
)
async def test_each_depends_on_form_blocks_while_its_issue_is_open(tmp_path, body, blocked_by):
    issue = _issue(31, "suite")
    issue["body"] = body
    gh = FakeGh(
        [issue],
        states={4: "CLOSED", 31: "OPEN", 412: "OPEN"},
        views={"qbtrix/photo-core#230": _open()},
    )

    found = await patrols.open_dependencies(str(tmp_path), [_src(31)], gh=gh)

    assert found == ({_src(31): blocked_by} if blocked_by else {})


async def test_an_unlisted_issue_body_is_viewed_once(tmp_path):
    """An issue missing from the open list is viewed once for its live body;
    a source repeated, or not a GitHub issue, costs nothing."""
    gh = FakeGh([], states={412: "OPEN"}, views={31: _open(body="Depends on #412")})

    found = await patrols.open_dependencies(str(tmp_path), [_src(31), "support", _src(31)], gh=gh)

    assert found == {_src(31): [f"{SLUG}#412"]}
    assert len(_views(gh)) == 1


@pytest.mark.parametrize(
    ("issues", "views", "blocked_by"),
    [
        ([], {}, ["?"]),  # the body is unreadable
        (
            [{**_issue(31, "suite"), "body": "Depends on: qbtrix/photo-core#230"}],
            {},
            ["qbtrix/photo-core#230"],
        ),
        ([{**_issue(31, "suite"), "body": "Depends on: #999"}], {}, [f"{SLUG}#999"]),
    ],
    ids=["body-unreadable", "cross-repo-unreadable", "state-unknown"],
)
async def test_a_gh_failure_counts_as_blocked(tmp_path, issues, views, blocked_by):
    gh = FakeGh(issues, states={31: "OPEN"}, views=views)

    found = await patrols.open_dependencies(str(tmp_path), [_src(31)], gh=gh)

    assert found == {_src(31): blocked_by}


async def test_off_github_nothing_is_checked(tmp_path):
    async def gitlab(argv):
        if "get-url" in argv:
            return 0, "https://gitlab.com/acme/app.git\n", ""
        raise AssertionError(f"unexpected argv {argv}")

    assert await patrols.open_dependencies(str(tmp_path), [_src(31)], gh=gitlab) == {}


# ---------------------------------------------------------------------------
# A note after the Depends on refs is not a dependency
# ---------------------------------------------------------------------------
# Live 2026-10-15: creative-apps #873 said "Depends on: none (#763 is lane-wide,
# not a dependency)" and never launched in 13 h: the #763 in the note blocked
# it. Only refs before a parenthesis or a " - " note count; "none" is none.


@pytest.mark.parametrize(
    ("body", "refs"),
    [
        ("Depends on: none (#763 is lane-wide, not a dependency)", []),
        ("**Depends on:** None - #763 is lane-wide", []),
        ("Depends on: none (#763 is lane-wide)\n#412 is a sibling, not a blocker", []),
        ("Depends on: #12 (see #763)", [f"{SLUG}#12"]),
        ("Depends on: #12, #13 - both lane-wide, see #763", [f"{SLUG}#12", f"{SLUG}#13"]),
        ("## Depends on\n\n- #12 (see #763)\n  - #13 - after #764\n", [f"{SLUG}#12", f"{SLUG}#13"]),
        ("Depends on: [#12](https://github.com/acme/app/issues/12)", [f"{SLUG}#12", "acme/app#12"]),
    ],
    ids=["none-paren", "none-dash", "none-ends-section", "ref-paren", "refs-dash", "list", "link"],
)
def test_a_depends_on_note_names_no_dependency(body, refs):
    assert patrols.depends_on_refs(body, SLUG) == refs


async def test_depends_on_none_with_a_note_is_not_blocked(tmp_path):
    issue = _issue(873, "feature-lead")
    issue["body"] = "Feature\n\nDepends on: none (#763 is lane-wide, not a dependency)"
    gh = FakeGh([issue], states={763: "OPEN", 873: "OPEN"})

    assert await patrols.open_dependencies(str(tmp_path), [_src(873)], gh=gh) == {}


async def test_depends_on_a_ref_with_a_note_blocks_on_the_ref_only(tmp_path):
    issue = _issue(31, "suite")
    issue["body"] = "Depends on: #12 (see #763)"
    gh = FakeGh([issue], states={12: "OPEN", 31: "OPEN", 763: "OPEN"})

    found = await patrols.open_dependencies(str(tmp_path), [_src(31)], gh=gh)

    assert found == {_src(31): [f"{SLUG}#12"]}
