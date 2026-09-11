# tests/ee/sites/test_delete_cascade.py — the ordered teardown (sites lifecycle
# wave 1 chunk 3).
#
# What is under test is mostly the ORDER and the LEDGER, because those are what make
# an irreversible multi-step destroy survivable. A cascade that does the right eight
# things in the wrong order is not a smaller bug than one that does seven.

from __future__ import annotations

import pytest
from pocketpaw_ee.sites.delete_cascade import (
    CASCADE_STEPS,
    OUTCOME_DONE,
    OUTCOME_LEGACY_RAIL,
    OUTCOME_SKIPPED,
    CascadeStepFailed,
    run_cascade,
)


class _Domain:
    def __init__(self, route="rt1", host="hn1"):
        self.cf_route_id = route
        self.cf_hostname_id = host


class _Site:
    def __init__(self, **kw):
        self.id = "s1"
        self.workspace = "w1"
        self.pocket_id = "pk1"
        self.script_name = "site-s1"
        self.deploy_target = "wfp"
        self.d1_database_id = "db1"
        self.signed_key = "key-abc"
        self.revoked = False
        self.subscription_status = "active"
        self.renewal_date = "2026-10-01"
        self.billing_rail = "credits"
        self.domains = [_Domain()]
        self.delete_ledger: dict[str, str] = {}
        self.__dict__.update(kw)


class _CF:
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

    async def delete_database(self, _id):
        await self._rec("delete_database")


class _Assets:
    def __init__(self):
        self.purged: list[tuple[str, str]] = []

    async def purge(self, *, workspace_id, pocket_id):
        self.purged.append((workspace_id, pocket_id))
        return 3


class _Deps:
    def __init__(self, cf=None, assets=None):
        self.cloudflare = cf or _CF()
        self.assets = assets if assets is not None else _Assets()
        self.records: list[tuple[str, str]] = []

    async def purge_records(self, *, workspace_id, site_id):
        self.records.append((workspace_id, site_id))


def _saver(saves):
    async def save(site):
        # Snapshot, not a reference — otherwise every entry is the final ledger and
        # the test cannot see what was persisted at each point.
        saves.append(dict(site.delete_ledger))

    return save


@pytest.mark.asyncio
async def test_every_step_runs_and_is_recorded_in_order() -> None:
    site, deps, saves = _Site(), _Deps(), []

    await run_cascade(site=site, deps=deps, save=_saver(saves))

    assert list(site.delete_ledger) == list(CASCADE_STEPS)
    assert all(v in (OUTCOME_DONE, OUTCOME_SKIPPED) for v in site.delete_ledger.values())


@pytest.mark.asyncio
async def test_billing_stops_before_anything_else_happens() -> None:
    """A teardown that fails partway must never keep taking money, so the cancel is
    step one and nothing destructive precedes it."""
    site, deps, saves = _Site(), _Deps(), []

    await run_cascade(site=site, deps=deps, save=_saver(saves))

    # The first thing ever persisted is the subscription step, alone.
    assert saves[0] == {"billing": OUTCOME_DONE}
    # The stop is LOCAL: the renewal sweeper only charges rows whose status is
    # "active", so clearing it is the whole mechanism. Nothing remote is called.
    assert site.subscription_status == "none"
    assert site.renewal_date is None


@pytest.mark.asyncio
async def test_the_key_dies_before_the_site_stops_serving() -> None:
    """Revoking is cheap and reversible-ish; deleting a Worker is neither. Doing it
    second means even a cascade that fails immediately after has already closed the
    surface that accepts data from the public internet."""
    site, deps, saves = _Site(), _Deps(), []

    await run_cascade(site=site, deps=deps, save=_saver(saves))

    order = list(site.delete_ledger)
    assert order.index("revoke") < order.index("script")
    assert site.signed_key == ""
    assert site.revoked is True


@pytest.mark.asyncio
async def test_resources_are_only_reclaimed_after_the_site_stops_serving() -> None:
    """A failure between these two leaves a database that costs money, rather than
    one still reachable from the internet."""
    site, deps, saves = _Site(), _Deps(), []

    await run_cascade(site=site, deps=deps, save=_saver(saves))

    order = list(site.delete_ledger)
    assert order.index("script") < order.index("d1") < order.index("r2")
    assert order.index("records") == len(order) - 1


@pytest.mark.asyncio
async def test_a_failure_stops_the_cascade_and_keeps_what_finished() -> None:
    """THE RESUME CONTRACT. Everything before the failure stays recorded, so a
    re-run skips it; nothing after it ran."""
    site = _Site()
    deps = _Deps(cf=_CF(fail_on="delete_worker"))

    with pytest.raises(CascadeStepFailed) as err:
        await run_cascade(site=site, deps=deps, save=_saver([]))

    assert err.value.step == "script"
    assert err.value.reason.startswith("script:")
    assert set(site.delete_ledger) == {"billing", "revoke", "routes", "hostnames"}
    # The reclaim steps must not have run behind a site that is still serving.
    assert "delete_database" not in deps.cloudflare.calls
    assert deps.records == []


@pytest.mark.asyncio
async def test_a_resumed_cascade_skips_finished_steps_instead_of_repeating_them() -> None:
    """Several steps are idempotent in fact but not in spirit — re-cancelling a
    subscription or re-charging a purge is not free even when it is safe."""
    site = _Site(delete_ledger={"billing": OUTCOME_DONE, "revoke": OUTCOME_DONE})
    deps = _Deps()

    await run_cascade(site=site, deps=deps, save=_saver([]))

    # The finished billing step is not re-run, so an already-active status is not
    # cleared a second time and the ledger is not rewritten.
    assert site.subscription_status == "active"
    assert list(site.delete_ledger) == list(CASCADE_STEPS)


@pytest.mark.asyncio
async def test_a_legacy_rail_is_flagged_but_still_has_its_local_renewal_stopped() -> None:
    """A row from before the credits rail may still be charged somewhere this
    package is forbidden to reach. Stopping the local renewal is what this code CAN
    do; the flag is what stops the rest becoming a customer billed for a site that
    no longer exists. Refusing outright would trap the site over a charge this code
    could not stop either way."""
    site = _Site(billing_rail="subscription")
    deps = _Deps()

    await run_cascade(site=site, deps=deps, save=_saver([]))

    assert site.delete_ledger["billing"] == OUTCOME_LEGACY_RAIL
    # Flagged, but the half that IS reachable still happened.
    assert site.subscription_status == "none"
    assert list(site.delete_ledger) == list(CASCADE_STEPS)


@pytest.mark.asyncio
async def test_an_empty_rail_is_legacy_because_that_is_what_legacy_rows_look_like() -> None:
    """THE case this flag exists for, and the one a truthiness check silently drops.

    Rows sold before Dodo was removed carry "addon" / "subscription" / "", and ""
    is the common shape — it is what every row written before `billing_rail` was
    introduced has. Under `if rail and rail != _CREDITS_RAIL` an empty rail is
    falsy, so it records OUTCOME_DONE with no warning and no operator follow-up:
    a customer still billed for a site that no longer exists, reported as healthy.

    MUTATION: restore `if rail and rail != _CREDITS_RAIL`.
    """
    site = _Site(billing_rail="")
    deps = _Deps()

    await run_cascade(site=site, deps=deps, save=_saver([]))

    assert site.delete_ledger["billing"] == OUTCOME_LEGACY_RAIL
    assert site.subscription_status == "none"


@pytest.mark.asyncio
async def test_the_plan_rail_is_current_and_carries_no_money_to_flag() -> None:
    """`plan` landed 2026-09-06, the day AFTER the credits cutover, so it is not a
    legacy rail. A plan-carried site has period_paid_usd = 0, no renewal_date and
    is invisible to the renewal sweep — flagging it sends an operator chasing a
    charge that does not exist, which is how a real flag stops being believed.

    MUTATION: drop _PLAN_RAIL from the predicate.
    """
    site = _Site(billing_rail="plan")
    deps = _Deps()

    await run_cascade(site=site, deps=deps, save=_saver([]))

    assert site.delete_ledger["billing"] == OUTCOME_DONE
    assert site.subscription_status == "none"


@pytest.mark.asyncio
async def test_a_free_site_has_no_billing_to_stop() -> None:
    site = _Site(subscription_status="none")
    deps = _Deps()

    await run_cascade(site=site, deps=deps, save=_saver([]))

    assert site.delete_ledger["billing"] == "nothing-to-do"


@pytest.mark.asyncio
async def test_the_worker_delete_follows_deploy_target_not_the_configured_mode() -> None:
    """404 is success for these calls, so aiming the delete at the wrong API path
    would record a completed teardown over a Worker that is still serving."""
    wfp, deps_wfp = _Site(deploy_target="wfp"), _Deps()
    await run_cascade(site=wfp, deps=deps_wfp, save=_saver([]))
    assert "delete_worker" in deps_wfp.cloudflare.calls
    assert "delete_account_script" not in deps_wfp.cloudflare.calls

    acct, deps_acct = _Site(deploy_target="workers"), _Deps()
    await run_cascade(site=acct, deps=deps_acct, save=_saver([]))
    assert "delete_account_script" in deps_acct.cloudflare.calls
    assert "delete_worker" not in deps_acct.cloudflare.calls


@pytest.mark.asyncio
async def test_a_local_site_owns_no_cloudflare_worker_to_delete() -> None:
    site = _Site(deploy_target="local")
    deps = _Deps()

    await run_cascade(site=site, deps=deps, save=_saver([]))

    assert site.delete_ledger["script"] == OUTCOME_SKIPPED
    assert deps.cloudflare.calls.count("delete_worker") == 0


@pytest.mark.asyncio
async def test_a_static_site_has_no_d1_and_says_so_rather_than_claiming_it_deleted_one() -> None:
    """ "Nothing was here" and "I removed it" are different facts. An operator reading
    a stalled cascade needs to tell them apart."""
    site = _Site(d1_database_id="", domains=[])
    deps = _Deps()

    await run_cascade(site=site, deps=deps, save=_saver([]))

    # Compared against the LITERAL, not the imported constant. Asserting
    # ``== OUTCOME_SKIPPED`` cannot detect the constant being collapsed into
    # "done", because the assertion moves with it.
    assert site.delete_ledger["d1"] == "nothing-to-do"
    assert site.delete_ledger["routes"] == "nothing-to-do"
    assert site.delete_ledger["hostnames"] == "nothing-to-do"
    assert "delete_database" not in deps.cloudflare.calls


@pytest.mark.asyncio
async def test_the_failure_reason_never_carries_provider_text() -> None:
    """``delete_reason`` is surfaced, so it holds a fixed token the way
    ``build_reason`` does — not a Cloudflare or driver sentence."""
    deps = _Deps(cf=_CF(fail_on="delete_database"))

    with pytest.raises(CascadeStepFailed) as err:
        await run_cascade(site=_Site(), deps=deps, save=_saver([]))

    assert err.value.reason == "d1:runtimeerror"
    assert "cloudflare said no" not in err.value.reason


@pytest.mark.asyncio
async def test_the_ledger_is_persisted_after_every_step_not_only_at_the_end() -> None:
    """A ledger written once at the end would record nothing about the crash it
    exists to survive."""
    saves: list[dict] = []

    await run_cascade(site=_Site(), deps=_Deps(), save=_saver(saves))

    assert len(saves) == len(CASCADE_STEPS)
    # Each save is strictly larger than the one before it.
    assert [len(s) for s in saves] == list(range(1, len(CASCADE_STEPS) + 1))


@pytest.mark.asyncio
async def test_a_deployment_with_no_public_asset_rail_skips_the_purge() -> None:
    site = _Site()
    deps = _Deps(assets=None)
    deps.assets = None

    await run_cascade(site=site, deps=deps, save=_saver([]))

    assert site.delete_ledger["r2"] == OUTCOME_SKIPPED


def test_nothing_to_do_and_done_stay_distinguishable() -> None:
    """The two outcomes must never collapse into one value.

    "There was no D1" and "I deleted the D1" are different facts, and an operator
    reading a stalled cascade has only the ledger to tell them apart. Pinned as
    literals so this survives someone redefining the constants.
    """
    assert OUTCOME_DONE == "done"
    assert OUTCOME_SKIPPED == "nothing-to-do"
    assert OUTCOME_LEGACY_RAIL == "legacy-rail-needs-operator"
    assert len({OUTCOME_DONE, OUTCOME_SKIPPED, OUTCOME_LEGACY_RAIL}) == 3
