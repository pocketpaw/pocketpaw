# tests/ee/sites/test_fault_ladder_deploy.py — the DEPLOY half of the fault ladder:
# rung F4 (the deploy answers 5xx) and rung F6 (the screenshot fails).
#
# Created 2026-08-10 (SG-7).
#
# WHY THESE TWO ARE IN A SEPARATE FILE FROM THE BUILD RUNGS. They are the only rungs on
# the ladder that inject into the REAL publish path (``sites.service.publish``), because
# they are the only ones whose boundary that path actually crosses today: the Daytona
# lane has no production callers on this branch, but Cloudflare and the screenshot are
# wired and shipping. So a green result here is worth strictly more than a green result
# in the build file — it is evidence about production behaviour, not about a module in
# waiting.
#
# THE PROPERTY BOTH RUNGS SHARE, and the reason they are worth testing at all: a publish
# that fails must fail WITHOUT damaging what is already live. A site that was serving
# yesterday's page must still be serving it — not a blank page, not a 404, and not a row
# claiming it deployed. Every assertion below is some version of that.
#
# ONE FINDING RECORDED HERE RATHER THAN ASSERTED, so it is not mistaken for verified: F4
# specifies "retry, and the previous deployment stays live". There is NO retry. A non-2xx
# from Cloudflare raises out of ``cloudflare_client._unwrap`` on the first attempt and
# nothing catches it to try again, which the attempt counters below MEASURE (they assert
# exactly one attempt). The "previous deployment stays live" half is real and is proven.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites import service as sites_service

from tests.ee.sites.faults import (
    FailingCloudflare,
    FailingWorkersDeploy,
    screenshot_always_fails,
)


class _FakeGenerator:
    """A generator that "builds" without a toolchain. Mirrors the fake the rest of the
    sites suite uses, so these tests exercise the deploy/persist half in isolation —
    generator faults are a different rung and belong to the build file."""

    def __init__(self) -> None:
        self.builds = 0

    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        self.builds += 1
        self.built = kw
        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


class _OkCloudflare:
    def __init__(self) -> None:
        self.put_calls: list[str] = []

    async def put_worker(self, *, script_name, bundle, bindings=None):
        self.put_calls.append(script_name)
        return True


async def _publish(pocket_id: str, **over):
    """Publish once with working fakes, unless a fault is passed in ``over``."""
    kwargs = {
        "workspace_id": "ws-fault",
        "user_id": "u1",
        "pocket_id": pocket_id,
        "ripple_spec": {"type": "container"},
        "theme": {"primary": "#0A84FF"},
        "name": "Bright Smile",
        "_generator": _FakeGenerator(),
        "_cloudflare": _OkCloudflare(),
        "_bundle_reader": lambda d: b"export default {}",
    }
    kwargs.update(over)
    return await sites_service.publish(**kwargs)


# ---------------------------------------------------------------------------
# F4 — the deploy answers 5xx
# ---------------------------------------------------------------------------


class TestF4DeployFailsAndThePreviousDeploymentSurvives:
    """A Cloudflare 5xx must not take the live site down with it.

    The failure this rung guards against is not the error — it is a row updated before
    the deploy is known to have worked. Flip ``deployed``/``url`` first and a CF outage
    silently repoints every visitor at a worker that was never uploaded.
    """

    async def test_a_5xx_deploy_raises_rather_than_reporting_success(self, beanie_test_db) -> None:
        cf = FailingCloudflare(status=503)
        with pytest.raises(ValidationError) as err:
            await _publish("pk-f4-raise", _cloudflare=cf)
        assert "cloudflare" in err.value.code.lower()
        assert cf.attempts == 1

    async def test_the_previous_deployment_is_untouched_by_a_failed_republish(
        self, beanie_test_db
    ) -> None:
        """The rung itself. Publish successfully, then fail a republish, then read the row
        back from the DB — not the returned object, which a failed publish never hands
        back — and assert the live pointer never moved.

        BOTH sides of the comparison are DB reads, deliberately. Comparing the row against
        the in-memory doc the first publish returned fails on a difference that is not the
        one under test: BSON truncates a datetime to milliseconds and drops the tzinfo, so
        the same instant reads unequal and the test reports a re-stamp that never
        happened.
        """
        first = await _publish("pk-f4-live")
        assert first.deployed is True
        site_id = first.id

        before = await sites_service._SiteDoc.find_one({"_id": site_id})
        assert before is not None
        was_url, was_at = before.url, before.deployed_at

        with pytest.raises(ValidationError):
            await _publish("pk-f4-live", _cloudflare=FailingCloudflare())

        after = await sites_service._SiteDoc.find_one({"_id": site_id})
        assert after is not None
        assert after.deployed is True, "a failed republish must not un-deploy a live site"
        assert after.url == was_url, "the live URL moved during a failed deploy"
        assert after.deployed_at == was_at, (
            "deployed_at was re-stamped for a deploy that never happened — the card "
            "would claim a deploy time for a site still serving the older build"
        )

    async def test_a_first_publish_that_fails_leaves_no_site_claiming_to_be_live(
        self, beanie_test_db
    ) -> None:
        """The other side of the same coin: with no previous deployment, a failed deploy
        must not leave a row that says it deployed. A site listed as live with nothing
        behind it is worse than an absent one — the user clicks through to a 404."""
        with pytest.raises(ValidationError):
            await _publish("pk-f4-first", _cloudflare=FailingCloudflare())

        status = await sites_service.pocket_status(workspace_id="ws-fault", pocket_id="pk-f4-first")
        assert status.is_live is False
        assert status.deployed_at is None

    async def test_the_workers_target_behaves_the_same_way(
        self, beanie_test_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rung proven on one deploy target is not proven on the others. ``workers`` is
        a different code path (``deploy_workers``, not ``cf.put_worker``), so it gets its
        own injection rather than an assumption of symmetry."""
        monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "workers")
        first = await _publish("pk-f4-workers", _workers_deploy=_ok_workers_deploy())
        was_url = first.url

        failing = FailingWorkersDeploy()
        with pytest.raises(ValidationError):
            await _publish("pk-f4-workers", _cloudflare=None, _workers_deploy=failing)
        assert failing.attempts == 1

        after = await sites_service._SiteDoc.find_one({"_id": first.id})
        assert after is not None
        assert after.url == was_url
        assert after.deployed is True

    async def test_a_failed_deploy_does_not_retry_today(self, beanie_test_db) -> None:
        """Recorded as a MEASUREMENT, not a wish. F4 asks for retry-with-backoff; the
        publish path has none, and ``cloudflare_client._unwrap`` raises on the first
        non-2xx. This test states the current behaviour so that adding a retry has to
        come here and say so, rather than the rung quietly reading as satisfied."""
        cf = FailingCloudflare()
        with pytest.raises(ValidationError):
            await _publish("pk-f4-noretry", _cloudflare=cf)
        assert cf.attempts == 1, (
            "put_worker was attempted more than once — a retry now exists, so F4's "
            "retry half is provable and must be proven (attempts, backoff, give-up)"
        )


def _ok_workers_deploy():
    async def _deploy(site_id: str, project_dir: str, **kw) -> str:
        return f"https://{site_id}.workers.dev"

    return _deploy


# ---------------------------------------------------------------------------
# F6 — the screenshot fails
# ---------------------------------------------------------------------------


class TestF6AScreenshotFailureNeverFailsThePublish:
    """The site is ALREADY live by the time the screenshot runs.

    That ordering is what makes this rung non-negotiable: anything escaping here fails a
    publish of a site that is deployed and serving, so the user sees an error for a
    publish that worked. And the work behind it is a paid, quota'd remote browser render —
    it will time out sooner or later, so "sooner or later" must be a missing thumbnail
    rather than a failed publish.
    """

    async def test_publish_succeeds_when_the_screenshot_raises(
        self, beanie_test_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        attempts = screenshot_always_fails(monkeypatch)
        site = await _publish("pk-f6-live")
        assert site.deployed is True
        assert attempts["attempts"] >= 1, (
            "the screenshot was never reached, so this proves nothing — the rung needs "
            "the capture to actually have failed"
        )

    async def test_the_deploy_is_fully_recorded_despite_the_failure(
        self, beanie_test_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not just "no exception": the row must be complete. A publish that swallowed the
        screenshot error but skipped the rest of its tail would pass a weaker assertion
        and still leave a half-written site."""
        screenshot_always_fails(monkeypatch)
        site = await _publish("pk-f6-recorded")

        after = await sites_service._SiteDoc.find_one({"_id": site.id})
        assert after is not None
        assert after.deployed is True
        assert after.deployed_at is not None
        assert after.signed_key

    async def test_the_site_is_still_reported_live(
        self, beanie_test_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What the user sees. A thumbnail is cosmetic; ``is_live`` is what the dashboard
        renders the site's state from, and it must not degrade over a picture."""
        screenshot_always_fails(monkeypatch)
        await _publish("pk-f6-status")
        status = await sites_service.pocket_status(
            workspace_id="ws-fault", pocket_id="pk-f6-status"
        )
        assert status.is_live is True

    async def test_a_republish_still_works_after_a_screenshot_failure(
        self, beanie_test_db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed capture must not leave anything that blocks the next publish — the
        same "permanently unpublishable" harm F3 exists to prevent, arriving by a
        different route."""
        screenshot_always_fails(monkeypatch)
        first = await _publish("pk-f6-again")
        second = await _publish("pk-f6-again")
        assert second.id == first.id
        assert second.deployed is True
