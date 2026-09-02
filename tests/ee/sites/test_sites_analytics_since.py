# tests/ee/sites/test_sites_analytics_since.py — SA-4, the stamp that says a Paw Site
# is currently counting its visitors and when that started: ``Site.analytics_since``.
#
# Created 2026-09-02 (feat/sites-analytics-read).
#
# The read endpoint has to tell FOUR situations apart, and three of them would
# otherwise render as the same panel of zeros. This field is what makes the second one
# knowable:
#
#   1. the plan does not buy analytics
#   2. it does, but no publish has ever deployed a counter — nothing was recorded
#   3. it does, a counter is up, and genuinely nobody visited
#   4. the read itself failed
#
# Nothing else on the document can separate 2 from 3. ``deployed_at`` says a publish
# happened, not that it carried a counter, so a site upgraded an hour ago is
# indistinguishable from a quiet site that has been counting for a month.
#
# TWO CLAIMS ARE ASSERTED HERE AND THEY ARE EASY TO CONFUSE.
#
# THE FIRST is that the stamp follows THE DEPLOY, not the plan. The counting rule has
# three parts — the site's plan, the operator kill switch, and whether the engine emits
# its own worker — and only the plan is visible at the publish seam. So the value is
# read off what the deploy left on disk, and the test for it drives an entitled site
# whose deploy wrote no counter and asserts no stamp. A publish path that re-derived
# the answer from the plan would fail exactly that case and pass every other one.
#
# THE SECOND is that the stamp is CLEARED by a publish that carries no counter. It is
# tempting to make the field write-once — it is called "since", after all — and that is
# the bug the paid → free → paid test exists to catch. A site that counted in June,
# dropped to free in July and re-upgraded today is entitled again with a June stamp,
# and yet nothing has recorded since the lapse because no publish has carried a
# counter. Left write-once, the read answers "counting since June, and nobody visited"
# over months in which nothing was counting at all. Cleared, entitled-with-no-stamp is
# exactly "you have not republished".
#
# The clock is FROZEN per publish rather than left to wall time. Windows' clock ticks
# about every 15.6 ms, so two publishes in one test can share a timestamp and a
# ``t2 > t1`` assertion would be flaky for reasons that have nothing to do with the
# code under test.

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.sites import analytics_worker  # noqa: E402
from pocketpaw_ee.sites import service as sites_service  # noqa: E402


class _FrozenClock(datetime):
    """A ``datetime`` whose ``now`` is scripted. Subclassed rather than mocked so every
    other use of the name inside ``sites.service`` — parsing, arithmetic, construction
    — keeps working while the publish path is patched."""

    _t: datetime = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):  # noqa: ARG003 - the publish path always passes UTC
        return cls._t


@pytest.fixture
def clock(monkeypatch):
    """Freeze ``sites.service``'s clock and hand the test the dial."""
    monkeypatch.setattr(sites_service, "datetime", _FrozenClock)

    def _set(moment: datetime) -> datetime:
        _FrozenClock._t = moment
        return moment

    _set(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
    return _set


class _FakeGenerator:
    """Stand-in for the SvelteKit generator — never touches Bun or workerd. Unlike the
    gate suite's, it returns a REAL directory, because the whole point of this file is
    what the deploy leaves in it."""

    def __init__(self, project_dir: Path) -> None:
        self._project_dir = project_dir

    async def build(self, **_kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        return BuildResult(project_dir=str(self._project_dir), ripple_version="0.2.0")


def _deployer(*, writes_counter: bool, seen: dict | None = None):
    """A workers deployer that reproduces the ONE disk effect the real one has:
    ``_write_deploy_files`` writes the generated entry when counting is on and deletes
    it when counting is off.

    ``writes_counter`` is deliberately INDEPENDENT of ``analytics_entitled``. The
    publish seam only knows the plan, and the deploy resolves two more things (the
    operator kill switch, and whether the engine already emits its own worker) — so a
    test has to be able to say "entitled, and yet no counter was deployed" or the claim
    that the stamp follows the artifact cannot be tested at all."""

    async def _deploy(site_id: str, project_dir: str, *, analytics_entitled=False, **_: object):
        if seen is not None:
            seen["analytics_entitled"] = analytics_entitled
        entry = Path(project_dir) / analytics_worker.ENTRY_FILENAME
        if writes_counter:
            entry.write_text("// a counter", encoding="utf-8")
        else:
            entry.unlink(missing_ok=True)
        return f"https://paw-site-{site_id}.acct.workers.dev"

    return _deploy


async def _publish(pocket_id: str, project_dir: Path, deployer):
    return await sites_service.publish(
        workspace_id="ws-analytics-since",
        user_id="u1",
        pocket_id=pocket_id,
        ripple_spec={"type": "container"},
        theme={},
        name="Since Site",
        _generator=_FakeGenerator(project_dir),
        _bundle_reader=lambda d: b"unused-in-workers-mode",
        _workers_deploy=deployer,
    )


async def _mark_paid(site_id, *, status: str = "active") -> None:
    doc = await sites_service._SiteDoc.find_one({"_id": site_id})
    assert doc is not None
    doc.plan_tier = "site"
    doc.subscription_status = status
    await doc.save()


async def _stamp(site_id):
    """The stored stamp, re-attached to UTC.

    Mongo stores a datetime with no zone, so a value written as tz-aware reads back
    NAIVE and compares unequal to the moment it was written. That is a property of the
    store rather than of this feature, and it is the same reason the read endpoint
    normalises before ``isoformat()`` — a bare naive stamp on the wire would be read as
    the client's local time."""
    doc = await sites_service._SiteDoc.find_one({"_id": site_id})
    assert doc is not None
    stamped = doc.analytics_since
    if stamped is not None and stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=UTC)
    return stamped


@pytest.fixture(autouse=True)
def _workers_mode(monkeypatch):
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "workers")
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)


@pytest.mark.asyncio
async def test_a_publish_that_deploys_a_counter_stamps_the_start(beanie_test_db, tmp_path, clock):
    """The happy case. A site is paying, the deploy put a counter in front of it, so the
    document records the moment recording began — which is what lets the read draw a
    series that starts where the data does instead of at the site's creation."""
    project = tmp_path / "project"
    project.mkdir()

    site = await _publish("pk-since-a", project, _deployer(writes_counter=False))
    await _mark_paid(site.id)

    moment = clock(datetime(2026, 6, 2, 9, 30, tzinfo=UTC))
    await _publish("pk-since-a", project, _deployer(writes_counter=True))

    assert await _stamp(site.id) == moment


@pytest.mark.asyncio
async def test_a_first_publish_that_deploys_a_counter_stamps_on_insert(
    beanie_test_db, tmp_path, clock, monkeypatch
):
    """THE INSERT BRANCH, which the other tests here never reach.

    Every case above publishes twice — the first publish is what creates the document,
    and a first publish resolves the plan against a document that does not exist yet, so
    it can never be entitled. That leaves the insert's stamp untested: a mutation
    hard-coding it to None escaped the first sweep, because the second publish takes the
    UPDATE branch and repairs it.

    It is reachable in production through the charge-first lane: ``activate_site``
    stamps the plan on payment confirmation and THEN deploys, so the deploy can carry a
    counter for a site whose row is being inserted by that same deploy. Without the
    stamp here that site reads as ``never_counted`` until something unrelated happens to
    republish it.

    Driven by making the deploy's artifact the source of truth — the deployer writes the
    entry — rather than by reproducing the whole charge-first sequence, which is
    ``activate_site``'s own tree and would test that seam instead of this one."""
    project = tmp_path / "project"
    project.mkdir()
    moment = clock(datetime(2026, 6, 10, 11, 0, tzinfo=UTC))

    site = await _publish("pk-since-first", project, _deployer(writes_counter=True))

    doc = await sites_service._SiteDoc.find_one({"_id": site.id})
    assert doc is not None, "this must be the INSERT branch"
    assert await _stamp(site.id) == moment


@pytest.mark.asyncio
async def test_a_publish_that_deploys_no_counter_leaves_it_unset(beanie_test_db, tmp_path, clock):
    """A free site's first publish. None is the honest value and it is what the read
    reports as ``never_counted`` — "nothing has been recorded", not "a quiet week"."""
    project = tmp_path / "project"
    project.mkdir()

    site = await _publish("pk-since-free", project, _deployer(writes_counter=False))

    assert await _stamp(site.id) is None


@pytest.mark.asyncio
async def test_the_stamp_follows_the_deploy_and_not_the_plan(beanie_test_db, tmp_path, clock):
    """THE CLAIM THE FIELD RESTS ON. The site is entitled and the publish seam passes
    that entitlement, and yet the deploy carried no counter — which is what the operator
    kill switch does, and what a SvelteKit site does by emitting its own worker. Nothing
    is recording, so nothing may be stamped.

    A publish path that stamped from ``site_analytics_entitled`` would pass every other
    test in this file and fail this one, which is the point of it."""
    project = tmp_path / "project"
    project.mkdir()
    seen: dict = {}

    site = await _publish("pk-since-killed", project, _deployer(writes_counter=False))
    await _mark_paid(site.id)

    clock(datetime(2026, 6, 3, 9, 0, tzinfo=UTC))
    await _publish("pk-since-killed", project, _deployer(writes_counter=False, seen=seen))

    assert seen["analytics_entitled"] is True, "the fixture must actually be entitled"
    assert await _stamp(site.id) is None


@pytest.mark.asyncio
async def test_a_republish_that_keeps_counting_keeps_the_original_stamp(
    beanie_test_db, tmp_path, clock
):
    """FIRST-SET-WINS WITHIN A COUNTING ERA. It is "since", not "last": re-stamping on
    every publish would move the start of the series forward and hide the history that
    is still sitting in the dataset."""
    project = tmp_path / "project"
    project.mkdir()

    site = await _publish("pk-since-again", project, _deployer(writes_counter=False))
    await _mark_paid(site.id)

    first = clock(datetime(2026, 6, 4, 8, 0, tzinfo=UTC))
    await _publish("pk-since-again", project, _deployer(writes_counter=True))
    assert await _stamp(site.id) == first

    clock(datetime(2026, 6, 20, 8, 0, tzinfo=UTC))
    await _publish("pk-since-again", project, _deployer(writes_counter=True))

    assert await _stamp(site.id) == first


@pytest.mark.asyncio
async def test_a_lapse_clears_the_stamp_and_the_re_upgrade_starts_a_new_era(
    beanie_test_db, tmp_path, clock
):
    """PAID → FREE → PAID, the sequence the whole clearing rule exists for.

    After the middle publish nothing is counting, so a stamp would be a claim about
    recording that is not happening. After the third, recording has started again — at
    the NEW date, because the June rows the old stamp pointed at were never joined to
    the September ones and, at three months' retention, are on their way out anyway."""
    project = tmp_path / "project"
    project.mkdir()

    site = await _publish("pk-since-lapse", project, _deployer(writes_counter=False))
    await _mark_paid(site.id)

    june = clock(datetime(2026, 6, 5, 10, 0, tzinfo=UTC))
    await _publish("pk-since-lapse", project, _deployer(writes_counter=True))
    assert await _stamp(site.id) == june

    # The subscription lapses. ``plan_tier`` is NOT reset by a cancellation — nothing
    # rewrites it — so this is the shape a real lapsed site has on disk.
    await _mark_paid(site.id, status="cancelled")
    clock(datetime(2026, 7, 5, 10, 0, tzinfo=UTC))
    await _publish("pk-since-lapse", project, _deployer(writes_counter=False))
    assert await _stamp(site.id) is None, "a publish with no counter must clear the stamp"

    await _mark_paid(site.id)
    september = clock(datetime(2026, 9, 2, 10, 0, tzinfo=UTC))
    await _publish("pk-since-lapse", project, _deployer(writes_counter=True))

    assert await _stamp(site.id) == september


@pytest.mark.asyncio
async def test_a_local_deploy_never_stamps_even_with_a_stale_entry_on_disk(
    beanie_test_db, tmp_path, clock, monkeypatch
):
    """THE REUSED WORKING DIRECTORY. A publish builds into the pocket's stable working
    dir, so the entry a workers publish wrote is still sitting there when the same site
    publishes to the local target. Only the workers branch can put a counter in front of
    a site, so the presence of that file says nothing on any other target — and a stamp
    taken from it would report a localhost preview as a live counting site."""
    project = tmp_path / "project"
    project.mkdir()
    (project / analytics_worker.ENTRY_FILENAME).write_text("// stale", encoding="utf-8")

    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "local")

    site = await sites_service.publish(
        workspace_id="ws-analytics-since",
        user_id="u1",
        pocket_id="pk-since-local",
        ripple_spec={"type": "container"},
        theme={},
        name="Since Site",
        _generator=_FakeGenerator(project),
        _bundle_reader=lambda d: b"unused",
        _local_deploy=lambda site_id, project_dir: f"http://127.0.0.1:8788/{site_id}/",
    )

    assert (project / analytics_worker.ENTRY_FILENAME).is_file(), "the stale entry must survive"
    assert await _stamp(site.id) is None
