# tests/ee/sites/test_agent_build_visibility.py — RX-4: what the CHAT AGENT is told
# about whether a site is actually live.
# Created: 2026-08-11 (feat/sites-react-edit-lane).
#
# THE DEFECT. ``_to_response`` gives the FRONTEND ``build_status`` / ``build_reason`` /
# ``build_job_id``, and the frontend polls them next to ``url`` knowing the wire contract
# that a site can be live and simultaneously mid-rebuild. The agent's publish tool
# hand-built its own five-key body and carried none of the three, so on react — the only
# engine with ``build_runs_async(engine) is True`` — it was handed values it had no way
# to interpret:
#
#   * FIRST publish: ``url=""`` and ``deployed=False``, while the create skill's STEP 4
#     tells it to "show the user the returned url".
#   * RE-publish: the PREVIOUS deploy's ``url`` and ``deployed=True``, so it reported the
#     old page as though the change were live.
#
# So these tests are about a derivation, not a feature: ``build_wire_state`` is the one
# place ``is_live`` is decided, and ``site_build_status`` is the later read that stops
# "queued" being a dead end (an async publish returns before the build starts, so
# without it the agent can never learn how the build ended).
#
# TWO PROPERTIES ARE LOAD BEARING:
#   (a) ``is_live`` requires url AND deployed AND no build in flight. Each of the three
#       is individually insufficient, and the tests below prove that one at a time
#       rather than asserting the happy path and trusting the rest.
#   (b) An UNKNOWN ``build_status`` reads as IN PROGRESS here — deliberately the
#       opposite of ``build_state.should_enqueue``, which treats unknown as terminal.
#       Both are right on their own axis; a test pins the direction so a future
#       "simplification" that unifies them fails loudly.
#
# Mutation coverage: tests/mutations/react_edit_lane.json.

from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.cloud.models.site import Site as _SiteDoc
from pocketpaw_ee.sites import build_job as bj
from pocketpaw_ee.sites import build_state as bs
from pocketpaw_ee.sites import service as sites_service

REACT_SOURCE = {"src/App.tsx": "export default function App() { return <p>hi</p>; }"}


class _Row:
    """The handful of Site fields ``build_wire_state`` reads. A plain object rather
    than a Beanie doc because the function is pure and must stay directly testable."""

    def __init__(self, **kw: Any) -> None:
        self.url = kw.get("url", "")
        self.deployed = kw.get("deployed", False)
        for key in ("build_status", "build_reason", "build_job_id"):
            if key in kw:
                setattr(self, key, kw[key])


# ---------------------------------------------------------------------------
# (a) is_live needs all three, and the raw fields pass through untouched
# ---------------------------------------------------------------------------


class TestIsLive:
    def test_a_settled_deploy_is_live(self) -> None:
        state = sites_service.build_wire_state(
            _Row(url="https://x.paw.test/", deployed=True, build_status="built")
        )
        assert state["is_live"] is True
        assert state["build_in_progress"] is False

    def test_a_first_async_publish_is_not_live_and_carries_no_url(self) -> None:
        """The exact row ``_enqueue_static_build`` inserts on a first publish. The agent
        used to receive this as a bare success with ``url=""``.

        THE MUTATION THAT BREAKS THIS: drop ``bool(url)`` from the ``is_live``
        conjunction. Run: an empty url read as live and this failed.
        """
        state = sites_service.build_wire_state(
            _Row(url="", deployed=False, build_status="queued", build_job_id="job-1")
        )
        assert state["is_live"] is False
        assert state["build_in_progress"] is True
        assert state["url"] == ""
        assert state["build_job_id"] == "job-1"

    def test_a_settled_build_with_no_url_is_still_not_live(self) -> None:
        """The row that makes ``bool(url)`` load bearing on its own: the build has
        SETTLED (nothing in flight) and ``deployed`` is True, so the only thing standing
        between the agent and handing the user an empty string is the url check.

        Not hypothetical. ``_canonical_site_doc`` exists partly because rows with a
        ``deployed`` flag and no persisted url are real — that is the stale-live-link bug
        it was written to dodge — and a deploy that flipped the flag before persisting
        the url lands in exactly this shape.

        THE MUTATION THAT BREAKS THIS: drop ``bool(url)`` from the ``is_live``
        conjunction. Run: the empty url read as live and this failed. (The queued-row
        test below does NOT catch that mutation — ``not in_progress`` already makes it
        false there, which is how the escape was found.)
        """
        state = sites_service.build_wire_state(_Row(url="", deployed=True, build_status="built"))
        assert state["build_in_progress"] is False
        assert state["is_live"] is False

    def test_a_republish_over_a_live_site_is_not_live_at_this_version(self) -> None:
        """The subtler half. A re-publish KEEPS the previous deploy's url and
        ``deployed=True`` on purpose, so a rebuild never reports a working site as down.
        Both of those say "live" while the url serves the PRE-CHANGE page, so
        ``build_status`` is the only thing that can tell the agent otherwise.

        THE MUTATION THAT BREAKS THIS: drop ``not in_progress`` from ``is_live``. Run:
        a mid-rebuild site reported live and this failed.
        """
        state = sites_service.build_wire_state(
            _Row(url="https://x.paw.test/", deployed=True, build_status="building")
        )
        assert state["is_live"] is False
        assert state["build_in_progress"] is True

    def test_a_never_built_pocket_is_not_live_and_not_in_progress(self) -> None:
        """ "none" is terminal AND means nothing was ever built, so it must not read as
        a build the agent could wait for."""
        state = sites_service.build_wire_state(_Row(url="", deployed=False))
        assert state["build_status"] == "none"
        assert state["build_in_progress"] is False
        assert state["is_live"] is False

    def test_a_failed_build_is_not_live_and_carries_its_reason(self) -> None:
        """``build_status="failed"`` with no reason is unactionable — the agent cannot
        tell "the user's code broke" from "we lost the container"."""
        state = sites_service.build_wire_state(
            _Row(url="", deployed=False, build_status="failed", build_reason="build:exit_1")
        )
        assert state["is_live"] is False
        assert state["build_in_progress"] is False
        assert state["build_reason"] == "build:exit_1"

    def test_no_site_doc_reads_as_nothing_built(self) -> None:
        """``None`` is a real input (a pocket that was never published), so the function
        answers it rather than making every caller write the empty shape."""
        state = sites_service.build_wire_state(None)
        assert state == {
            "url": "",
            "deployed": False,
            "build_status": "none",
            "build_reason": None,
            "build_job_id": None,
            "build_in_progress": False,
            "is_live": False,
        }

    def test_a_pre_sl3_row_with_no_build_fields_does_not_raise(self) -> None:
        """Read through ``getattr`` defaults like ``_to_response`` does, so a row written
        before the build fields existed reads as "no build" instead of blowing up."""
        bare = type("Bare", (), {"url": "https://x/", "deployed": True})()
        state = sites_service.build_wire_state(bare)
        assert state["build_status"] == "none"
        assert state["is_live"] is True


# ---------------------------------------------------------------------------
# (b) the unknown-status direction, and that it is NOT should_enqueue's
# ---------------------------------------------------------------------------


class TestUnknownStatusReadsAsInProgress:
    def test_an_unrecognised_status_is_in_progress_not_live(self) -> None:
        """The wire's contract: a client treats an unrecognised status as in-progress, so
        growing the vocabulary never shows a user a spurious "live". A status this deploy
        predates must not be mapped to "nothing is building".

        THE MUTATION THAT BREAKS THIS: derive ``build_in_progress`` from
        ``IN_FLIGHT_STATUSES`` instead of from ``TERMINAL_STATUSES``. Run: the unknown
        status read as terminal, the site reported live, and this failed.
        """
        state = sites_service.build_wire_state(
            _Row(url="https://x/", deployed=True, build_status="uploading-artifact")
        )
        assert state["build_in_progress"] is True
        assert state["is_live"] is False
        # And the raw value is passed through VERBATIM, never normalised.
        assert state["build_status"] == "uploading-artifact"

    def test_the_two_readers_disagree_on_purpose(self) -> None:
        """The asymmetry stated out loud so nobody "fixes" it into one helper.

        ``should_enqueue`` treats an unknown status as TERMINAL and starts a build (a
        redundant build costs one sandbox; a stuck guard costs the site every future
        publish). This function treats the same status as IN PROGRESS (a spurious "your
        site is live" costs the user's trust). Same input, opposite answers, both right.
        """
        row = _Row(url="https://x/", deployed=True, build_status="something-new")
        assert bs.should_enqueue(row, 600) is True
        assert sites_service.build_wire_state(row)["build_in_progress"] is True

    def test_every_in_flight_status_reads_as_in_progress(self) -> None:
        """Derived from the state machine rather than hand-listed, so a new in-flight
        state is covered here the moment it joins ``IN_FLIGHT_STATUSES``."""
        for status in sorted(bs.IN_FLIGHT_STATUSES):
            state = sites_service.build_wire_state(
                _Row(url="https://x/", deployed=True, build_status=status)
            )
            assert state["build_in_progress"] is True, status
            assert state["is_live"] is False, status


# ---------------------------------------------------------------------------
# site_build_status — the later read that stops "queued" being a dead end
# ---------------------------------------------------------------------------


async def _seed_site(**kw: Any) -> _SiteDoc:
    doc = _SiteDoc(
        workspace=kw.pop("workspace", "ws1"),
        pocket_id=kw.pop("pocket_id", "pk1"),
        owner="u1",
        name=kw.pop("name", "Bright Smile"),
        script_name="s1",
        deployed=kw.pop("deployed", False),
        url=kw.pop("url", ""),
        signed_key="k",
        **kw,
    )
    await doc.insert()
    return doc


@pytest.mark.asyncio
async def test_status_reads_back_a_settled_build(beanie_test_db):
    """The turn after the publish: the build finished, so the agent can finally show a
    url. This is the whole point of the tool."""
    await _seed_site(
        pocket_id="pk1",
        url="https://bright.paw.test/",
        deployed=True,
        build_status="built",
        build_job_id="job-9",
    )

    out = await sites_service.site_build_status(workspace_id="ws1", pocket_id="pk1")

    assert out["published"] is True
    assert out["is_live"] is True
    assert out["build_in_progress"] is False
    assert out["build_status"] == "built"
    assert out["url"] == "https://bright.paw.test/"
    assert out["name"] == "Bright Smile"
    assert out["site_id"]


@pytest.mark.asyncio
async def test_status_reads_back_an_in_flight_build(beanie_test_db):
    await _seed_site(pocket_id="pk1", url="", deployed=False, build_status="queued")

    out = await sites_service.site_build_status(workspace_id="ws1", pocket_id="pk1")

    assert out["build_in_progress"] is True
    assert out["is_live"] is False


@pytest.mark.asyncio
async def test_status_of_a_never_published_pocket_says_so(beanie_test_db):
    """Not an error. "This was never published" is the useful answer, and it is correct
    whether the pocket has no site or does not exist."""
    out = await sites_service.site_build_status(workspace_id="ws1", pocket_id="nope")

    assert out["published"] is False
    assert out["is_live"] is False
    assert out["site_id"] is None


@pytest.mark.asyncio
async def test_status_is_tenant_scoped(beanie_test_db):
    """The tenancy filter IS the access check on this read, so it gets a test rather
    than a comment. Another workspace's site must read as never-published.

    THE MUTATION THAT BREAKS THIS: drop the ``workspace`` filter from
    ``_canonical_site_doc``'s query.
    """
    await _seed_site(workspace="ws_other", pocket_id="pk1", url="https://x/", deployed=True)

    out = await sites_service.site_build_status(workspace_id="ws1", pocket_id="pk1")

    assert out["published"] is False
    assert out["is_live"] is False


# ---------------------------------------------------------------------------
# End to end: a real async react publish reports queued, not a bare empty url
# ---------------------------------------------------------------------------


class _FakeGenerator:
    def __init__(self) -> None:
        self.build_calls: list[dict[str, Any]] = []

    async def build(self, **kw: Any) -> Any:
        from pocketpaw_ee.sites.generator_client import BuildResult

        self.build_calls.append(kw)
        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


class _FakePool:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def enqueue_job(self, function: str, *args: Any, _job_id: str | None = None, **kw: Any):
        self.calls.append({"function": function, "job_id": _job_id})
        return object()


@pytest.mark.asyncio
async def test_a_real_first_react_publish_reports_queued_not_a_url(
    beanie_test_db, monkeypatch: pytest.MonkeyPatch
):
    """The end-to-end version of the first-publish case, through the REAL async publish
    path rather than a hand-built row — so the shape this asserts is the shape
    ``_enqueue_static_build`` actually produces.

    The inline generator must not have run (that is the flip), and the doc the publish
    returns must derive to "not live, build in progress" rather than to a success with
    an empty url.
    """
    from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

    pocket = _PocketDoc(workspace="ws1", name="P", owner="u1", type="site")
    await pocket.insert()

    pool = _FakePool()

    async def _get_pool() -> Any:
        return pool

    monkeypatch.setattr(bj, "_get_pool", _get_pool)
    gen = _FakeGenerator()

    doc = await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=str(pocket.id),
        theme={},
        name="x",
        engine="react",
        source=REACT_SOURCE,
        _generator=gen,
        _local_deploy=lambda site_id, project_dir: f"http://127.0.0.1/{site_id}/",
    )

    # The build was enqueued, not run inline.
    assert gen.build_calls == []
    assert pool.calls, "no build was enqueued"

    state = sites_service.build_wire_state(doc)
    assert state["is_live"] is False, state
    assert state["build_in_progress"] is True, state
    assert state["url"] == ""
    assert state["build_status"] in bs.IN_FLIGHT_STATUSES
    # And the later read agrees with what the publish returned — one derivation.
    later = await sites_service.site_build_status(workspace_id="ws1", pocket_id=str(pocket.id))
    assert later["is_live"] is False
    assert later["build_status"] == state["build_status"]
