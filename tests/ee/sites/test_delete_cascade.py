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
    OUTCOME_SKIPPED,
    OUTCOME_UNCANCELLABLE,
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
        self.subscription_id = "sub_live"
        self.subscription_status = "active"
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
    def __init__(self, cf=None, assets=None, fail_cancel=False):
        self.cloudflare = cf or _CF()
        self.assets = assets if assets is not None else _Assets()
        self.cancelled: list[str] = []
        self.records: list[tuple[str, str]] = []
        self.fail_cancel = fail_cancel

    async def cancel_subscription(self, sub_id):
        if self.fail_cancel:
            raise RuntimeError("gateway down")
        self.cancelled.append(sub_id)

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
    assert saves[0] == {"subscription": OUTCOME_DONE}
    assert deps.cancelled == ["sub_live"]


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
    assert set(site.delete_ledger) == {"subscription", "revoke", "routes", "hostnames"}
    # The reclaim steps must not have run behind a site that is still serving.
    assert "delete_database" not in deps.cloudflare.calls
    assert deps.records == []


@pytest.mark.asyncio
async def test_a_resumed_cascade_skips_finished_steps_instead_of_repeating_them() -> None:
    """Several steps are idempotent in fact but not in spirit — re-cancelling a
    subscription or re-charging a purge is not free even when it is safe."""
    site = _Site(delete_ledger={"subscription": OUTCOME_DONE, "revoke": OUTCOME_DONE})
    deps = _Deps()

    await run_cascade(site=site, deps=deps, save=_saver([]))

    assert deps.cancelled == []  # not cancelled a second time
    assert list(site.delete_ledger) == list(CASCADE_STEPS)


@pytest.mark.asyncio
async def test_an_uncancellable_subscription_is_recorded_and_does_not_block() -> None:
    """A row predating the activation webhook holds a checkout SESSION id the
    gateway rejects. Refusing to delete would trap the customer's site forever over
    a subscription we cannot reach either way."""
    site = _Site(subscription_id="cks_legacy")
    deps = _Deps()

    await run_cascade(site=site, deps=deps, save=_saver([]))

    assert site.delete_ledger["subscription"] == OUTCOME_UNCANCELLABLE
    assert deps.cancelled == []
    assert list(site.delete_ledger) == list(CASCADE_STEPS)


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
    deps = _Deps(fail_cancel=True)

    with pytest.raises(CascadeStepFailed) as err:
        await run_cascade(site=_Site(), deps=deps, save=_saver([]))

    assert err.value.reason == "subscription:runtimeerror"
    assert "gateway down" not in err.value.reason


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
    assert OUTCOME_UNCANCELLABLE == "uncancellable"
    assert len({OUTCOME_DONE, OUTCOME_SKIPPED, OUTCOME_UNCANCELLABLE}) == 3
