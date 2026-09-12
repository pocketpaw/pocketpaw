# tests/ee/sites/test_pause_lane.py — pause, resume, and the billing deferral
# (sites lifecycle wave 4 chunk 14).
#
# Created 2026-09-12 (feat/sites-pause).
#
# WHAT IS ACTUALLY UNDER TEST here is not "does pause call Cloudflare" — it is the
# three properties that make pause the REVERSIBLE answer to delete, each of which
# fails silently if it is wrong:
#
#   1. Pause runs the serving steps and NONE of the reclaims. A pause that reached the
#      D1 step would destroy the customer's data while reporting a reversible action.
#   2. Pause writes its OWN ledger. Sharing ``delete_ledger`` would make a later delete
#      skip revoking a key the resume had re-minted, leaving a live public ingest
#      surface on a destroyed site — a security bug reached through a bookkeeping one.
#   3. Billing DEFERS. A renewal that fired on a paused site charges for a page nobody
#      can reach; one that was cancelled instead makes resume a repurchase.
#
# The mutations that break these live in tests/mutations/sites_pause.json and every
# one of them has been run and observed to fail a test here.

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pocketpaw_ee.sites.delete_cascade import (
    OUTCOME_DONE,
    OUTCOME_SKIPPED,
    STEP_D1,
    STEP_R2,
    STEP_RECORDS,
    CascadeStepFailed,
)
from pocketpaw_ee.sites.pause import (
    PAUSE_STEPS,
    deferred_renewal_date,
    remint_signed_key,
    restore_serving,
    run_pause,
)


class _Domain:
    def __init__(self, host="shop.example.com"):
        self.hostname = host
        self.cf_route_id = "rt1"
        self.cf_hostname_id = "hn1"
        self.cname_target = "old.target"
        self.status = "live"


class _Site:
    def __init__(self, **kw):
        self.id = "s1"
        self.workspace = "w1"
        self.pocket_id = "pk1"
        self.plan_tier = None
        self.script_name = "paw-site-s1"
        self.deploy_target = "wfp"
        self.deployed = True
        self.d1_database_id = "db1"
        self.signed_key = "site_key_old"
        self.revoked = False
        self.subscription_status = "active"
        self.renewal_date = None
        self.paused_at = None
        self.billing_rail = "credits"
        self.domains = [_Domain()]
        self.delete_ledger: dict[str, str] = {}
        self.pause_ledger: dict[str, str] = {}
        self.__dict__.update(kw)


class _CF:
    """Records calls. Carries ONLY the methods pause is allowed to reach.

    A reclaim step would call ``delete_database`` / ``purge`` and raise AttributeError
    here rather than passing, which is the point: the double's SHAPE is half the
    assertion.
    """

    def __init__(self, fail_on: str | None = None):
        self.calls: list[str] = []
        self.fail_on = fail_on

    async def _rec(self, name):
        if self.fail_on == name:
            raise RuntimeError("cloudflare said no")
        self.calls.append(name)

    async def delete_worker_route(self, _id):
        await self._rec("delete_worker_route")

    async def delete_custom_hostname(self, _id):
        await self._rec("delete_custom_hostname")

    async def delete_worker(self, _name):
        await self._rec("delete_worker")

    async def delete_account_script(self, _name):
        await self._rec("delete_account_script")


class _Deps:
    def __init__(self, cf):
        self.cloudflare = cf


async def _save(_site):
    return None


# ---------------------------------------------------------------------------
# 1. The step set
# ---------------------------------------------------------------------------


def test_pause_runs_the_serving_steps_and_none_of_the_reclaims():
    """The whole feature is this list. Mutation: add STEP_D1 to PAUSE_STEPS."""
    assert PAUSE_STEPS == ("revoke", "routes", "hostnames", "script")
    for reclaim in (STEP_D1, STEP_R2, STEP_RECORDS):
        assert reclaim not in PAUSE_STEPS
    # And billing, which delete stops FIRST, is not paused by tearing it down.
    assert "billing" not in PAUSE_STEPS


async def test_a_pause_takes_the_site_down_without_touching_the_data():
    site = _Site()
    cf = _CF()
    await run_pause(site=site, deps=_Deps(cf), save=_save)

    assert cf.calls == ["delete_worker_route", "delete_custom_hostname", "delete_worker"]
    # The key is gone, so the public ingest surface is closed...
    assert site.signed_key == ""
    assert site.revoked is True
    # ...and everything that holds customer data is untouched.
    assert site.d1_database_id == "db1"
    assert site.subscription_status == "active"


async def test_pause_writes_its_own_ledger_and_never_the_deletes():
    """Mutation: point ``PAUSE_LEDGER_FIELD`` at ``delete_ledger``.

    A shared ledger is not an untidiness. A paused-then-resumed site carrying
    ``revoke: done`` in ``delete_ledger`` makes a LATER delete skip revoking the key
    the resume re-minted, so a destroyed site keeps a live, signed, publicly
    reachable capture endpoint.
    """
    site = _Site()
    await run_pause(site=site, deps=_Deps(_CF()), save=_save)

    assert set(site.pause_ledger) == set(PAUSE_STEPS)
    assert site.delete_ledger == {}


async def test_a_failed_pause_records_where_it_stopped_and_a_retry_resumes():
    site = _Site()
    cf = _CF(fail_on="delete_worker")
    with pytest.raises(CascadeStepFailed) as caught:
        await run_pause(site=site, deps=_Deps(cf), save=_save)
    assert caught.value.step == "script"
    assert set(site.pause_ledger) == {"revoke", "routes", "hostnames"}

    # The retry skips what finished rather than re-running it.
    healthy = _CF()
    await run_pause(site=site, deps=_Deps(healthy), save=_save)
    assert healthy.calls == ["delete_worker"]


async def test_a_site_with_no_domains_pauses_by_deleting_only_its_worker():
    site = _Site(domains=[])
    cf = _CF()
    await run_pause(site=site, deps=_Deps(cf), save=_save)
    assert cf.calls == ["delete_worker"]
    assert site.pause_ledger["routes"] == OUTCOME_SKIPPED
    assert site.pause_ledger["hostnames"] == OUTCOME_SKIPPED
    assert site.pause_ledger["script"] == OUTCOME_DONE


# ---------------------------------------------------------------------------
# 2. Billing: deferral, not cancellation
# ---------------------------------------------------------------------------


def test_resume_defers_the_renewal_by_exactly_the_dark_window():
    """Mutation: return ``current`` unchanged from ``deferred_renewal_date``.

    Nine dark days must move the renewal nine days. Anything else either bills the
    customer for a site nobody could reach or hands them free service.
    """
    paused = datetime(2026, 9, 1, tzinfo=UTC)
    site = _Site(renewal_date=datetime(2026, 9, 20, tzinfo=UTC), paused_at=paused)
    moved = deferred_renewal_date(site, now=paused + timedelta(days=9))
    assert moved == datetime(2026, 9, 29, tzinfo=UTC)


def test_a_free_site_has_no_renewal_to_defer():
    site = _Site(renewal_date=None, paused_at=datetime(2026, 9, 1, tzinfo=UTC))
    assert deferred_renewal_date(site) is None


def test_a_row_with_no_pause_stamp_keeps_its_renewal_date():
    """Guessing a window is worse than leaving it: long gives away service, short
    bills for darkness."""
    due = datetime(2026, 9, 20, tzinfo=UTC)
    site = _Site(renewal_date=due, paused_at=None)
    assert deferred_renewal_date(site) == due


def test_a_backwards_clock_never_moves_the_renewal_earlier():
    paused = datetime(2026, 9, 10, tzinfo=UTC)
    due = datetime(2026, 9, 20, tzinfo=UTC)
    site = _Site(renewal_date=due, paused_at=paused)
    assert deferred_renewal_date(site, now=paused - timedelta(days=2)) == due


def test_the_renewal_sweeper_skips_a_paused_site():
    """The billing half of pause, asserted on the QUERY rather than on a run.

    Mutation: drop the ``lifecycle_state`` clause from the sweeper's filter. The
    charge is a local write against ``subscription_status``, so a paused site left in
    the selection renews silently and the customer pays for a dark page.
    """
    import inspect

    from pocketpaw_ee.sites import renewal_sweeper

    source = inspect.getsource(renewal_sweeper.sweep_site_renewals)
    assert '"lifecycle_state"' in source
    # A positive match on the chargeable states, so a state nobody has invented yet
    # does not start billing by default.
    assert '"$in": ["live", "resuming", None]' in source
    assert "paused" not in source.split('"lifecycle_state"')[1].split("}")[0]


# ---------------------------------------------------------------------------
# 3. Resume's restore half
# ---------------------------------------------------------------------------


class _Created:
    def __init__(self, hostname):
        self.id = "hn-new"
        self.hostname = hostname
        self.cname_target = "new.target"
        self.status = type("S", (), {"value": "pending"})()


class _RestoreCF:
    def __init__(self, fail_on: str | None = None):
        self.calls: list[str] = []
        self.fail_on = fail_on

    async def create_custom_hostname(self, hostname, *, features=None):
        if self.fail_on == "hostname":
            raise RuntimeError("cloudflare said no")
        self.calls.append("create_custom_hostname")
        return _Created(hostname)

    async def create_worker_route(self, *, pattern, script):
        if self.fail_on == "route":
            raise RuntimeError("cloudflare said no")
        self.calls.append(f"create_worker_route:{pattern}:{script}")
        return "rt-new"


async def test_resume_recreates_the_hostname_before_the_route():
    """The cascade tears down route-then-hostname; putting back inverts it.

    Mutation: swap the two entries in ``restore_serving``'s step tuple. A route
    created against a hostname the zone does not hold yet is the failure this
    ordering avoids, and it is invisible in a mock that does not care about order.
    """
    site = _Site(deploy_target="workers", deployed=True)
    cf = _RestoreCF()
    ledger = await restore_serving(site=site, deps=_Deps(cf), save=_save)

    assert cf.calls[0] == "create_custom_hostname"
    assert cf.calls[1].startswith("create_worker_route:shop.example.com/*:paw-site-")
    assert ledger["resume:hostnames"] == OUTCOME_DONE
    assert ledger["resume:routes"] == OUTCOME_DONE


async def test_a_resumed_hostname_comes_back_at_cloudflares_real_status():
    """Not an optimistic "live". The certificate went with the old binding."""
    site = _Site(deploy_target="workers", deployed=True)
    await restore_serving(site=site, deps=_Deps(_RestoreCF()), save=_save)
    assert site.domains[0].status == "pending"
    assert site.domains[0].cf_hostname_id == "hn-new"
    assert site.domains[0].cname_target == "new.target"


async def test_a_wfp_site_gets_its_hostname_back_but_no_route():
    """A wfp script is not route-addressable, so a route naming it would 404 — and
    404 reads as success on these calls, which is how a resume reports a restored
    domain over a dark one."""
    site = _Site(deploy_target="wfp", deployed=True)
    cf = _RestoreCF()
    ledger = await restore_serving(site=site, deps=_Deps(cf), save=_save)
    assert cf.calls == ["create_custom_hostname"]
    assert ledger["resume:routes"] == OUTCOME_SKIPPED


async def test_a_half_finished_restore_resumes_rather_than_repeating():
    site = _Site(deploy_target="workers", deployed=True)
    with pytest.raises(CascadeStepFailed) as caught:
        await restore_serving(site=site, deps=_Deps(_RestoreCF(fail_on="route")), save=_save)
    assert caught.value.step == "resume:routes"
    assert site.pause_ledger["resume:hostnames"] == OUTCOME_DONE

    healthy = _RestoreCF()
    await restore_serving(site=site, deps=_Deps(healthy), save=_save)
    assert [c.split(":")[0] for c in healthy.calls] == ["create_worker_route"]


def test_resume_mints_a_new_key_and_never_restores_the_old_one():
    """A key that survived a pause would still be embedded in cached copies of the
    old page, so re-honouring it means a site that was dark for a month accepts
    ingest signed from before it went down."""
    site = _Site(signed_key="", revoked=True)
    minted = remint_signed_key(site)
    assert minted.startswith("site_key_")
    assert minted != "site_key_old"
    assert site.signed_key == minted
    assert site.revoked is False


def test_lifecycle_state_is_actually_populated_on_the_site_response():
    """Mutation: delete the ``lifecycle_state=`` line from ``_to_response``.

    Declaring a field on the DTO and never passing it is a mistake this file has
    already made once — ``build_status`` shipped on ``SiteResponse`` with nothing
    populating it and read its default forever. The gallery badge is this field's
    only consumer, so the same omission renders every paused site as live.
    """
    import inspect

    from pocketpaw_ee.sites import service

    source = inspect.getsource(service._to_response)
    assert "lifecycle_state=" in source


# ---------------------------------------------------------------------------
# 4. Pause meets transfer — the two lanes that both walk ``domains``
# ---------------------------------------------------------------------------


class _XferSite:
    def __init__(self, **kw):
        self.workspace = "w1"
        self.owner = "u1"
        self.delete_status = "none"
        self.transfer_status = "none"
        self.lifecycle_state = "live"
        self.subscription_status = "none"
        self.plan_tier = None
        self.billing_rail = "credits"
        self.__dict__.update(kw)


class _Settings:
    site_transfers_allowed = True


def _offer(site):
    from pocketpaw_ee.sites import transfer as transfer_mod

    transfer_mod.check_can_offer(
        site=site,
        actor_user_id="u1",
        source_settings=_Settings(),
        destination_workspace_id="w2",
    )


@pytest.mark.parametrize("state", ["pausing", "resuming"])
def test_a_site_mid_pause_or_mid_resume_cannot_be_transferred(state):
    """Mutation: drop the lifecycle guard from ``check_can_offer``.

    Both lanes write this row's ``domains`` list and call Cloudflare against the ids
    on it. A transfer landing inside a pause moves the document out from under a
    cascade still writing to it, and the two then disagree about which workspace owns
    the hostnames being created.
    """
    from pocketpaw_ee.sites import transfer as transfer_mod

    with pytest.raises(transfer_mod.TransferRefused) as caught:
        _offer(_XferSite(lifecycle_state=state))
    assert caught.value.code == "transfer.lifecycle_in_flight"


def test_a_settled_paused_site_is_still_transferable():
    """Deliberately allowed, and the cheapest kind of site to move: no Worker, no
    routes, no hostname bindings, so nothing is serving for a move to interrupt."""
    _offer(_XferSite(lifecycle_state="paused"))


def test_the_two_in_flight_vocabularies_cannot_drift():
    """``transfer`` spells the in-flight states itself rather than importing them, to
    avoid an import cycle. This is what stops the copy going stale."""
    from pocketpaw_ee.sites import pause as pause_lane
    from pocketpaw_ee.sites import transfer as transfer_mod

    assert set(transfer_mod._LIFECYCLE_IN_FLIGHT) == set(pause_lane.IN_FLIGHT_STATES)


@pytest.mark.parametrize("status", ["offered", "in_flight"])
def test_pause_and_resume_refuse_while_a_transfer_is_open(status):
    """The mirror half. Mutation: delete either ``_refuse_if_transfer_open`` call.

    ``offered`` counts as well as ``in_flight``: an open offer is a decision the
    destination has not answered, and taking the site off the internet underneath it
    changes what they are being asked to accept.
    """
    from pocketpaw_ee.cloud._core.errors import ConflictError
    from pocketpaw_ee.sites import service

    for verb in ("pause", "resume"):
        with pytest.raises(ConflictError) as caught:
            service._refuse_if_transfer_open(_XferSite(transfer_status=status), verb)
        assert caught.value.code == "site.transfer_open"

    import inspect

    for fn in (service.pause_site, service.resume_site):
        assert "_refuse_if_transfer_open(site," in inspect.getsource(fn)


def test_a_site_with_no_open_transfer_pauses_freely():
    from pocketpaw_ee.sites import service

    for status in ("none", "failed", ""):
        service._refuse_if_transfer_open(_XferSite(transfer_status=status), "pause")
