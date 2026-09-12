# tests/ee/sites/test_delete_job.py — the delete job: the forced export, the cascade,
# and the row (sites lifecycle wave 1, feat/sites-delete-endpoint).
#
# THE CENTRAL ASSERTION OF THIS FILE is that nothing destructive runs before an export
# exists. The captain's requirement, the cascade's header and the shipped client's
# ``export:`` failure sentence all say the same thing from three directions, and none of
# them was enforced anywhere until this job. So the ordering tests here are not
# belt-and-braces: they are the only thing standing between "we took a copy first" and a
# sentence in a doc.
#
# The second thing under test is quieter and was a live bug during this build: the
# cascade stops billing by clearing ``subscription_status`` IN MEMORY, so a save callback
# that persisted only ``delete_ledger`` would leave a half-torn-down site whose ledger
# says billing is done and whose row still bills. See ``_CASCADE_MUTATED_FIELDS``.

from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.models.lead import Lead, LeadSource
from pocketpaw_ee.cloud.models.site import Site, SiteDomain
from pocketpaw_ee.cloud.models.site_rate_counter import SiteRateCounter
from pocketpaw_ee.sites import delete_job as delete_job_mod
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.delete_cascade import CASCADE_STEPS, CascadeStepFailed
from pocketpaw_ee.sites.export import ExportUnavailable

WORKSPACE = "ws_owner"
OWNER = "u-owner"


class _CF:
    """Records the Cloudflare calls the cascade makes, and can refuse one."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[str] = []
        self.fail_on = fail_on

    def __getattr__(self, name: str) -> Any:
        async def _call(*_a: Any, **_kw: Any) -> None:
            if self.fail_on == name:
                raise RuntimeError("cloudflare said no")
            self.calls.append(name)

        return _call


class _Assets:
    def __init__(self) -> None:
        self.purged: list[tuple[str, str]] = []

    async def purge(self, *, workspace_id: str, pocket_id: str) -> int:
        self.purged.append((workspace_id, pocket_id))
        return 0


class _Deps:
    def __init__(self, cf: _CF | None = None) -> None:
        self.cloudflare = cf or _CF()
        self.assets = _Assets()
        self.purged_records: list[tuple[str, str]] = []

    async def purge_records(self, *, workspace_id: str, site_id: str) -> None:
        self.purged_records.append((workspace_id, site_id))


async def _seed(**kw: Any) -> Site:
    doc = Site(
        workspace=kw.pop("workspace", WORKSPACE),
        pocket_id=kw.pop("pocket_id", "pk1"),
        owner=kw.pop("owner", OWNER),
        name=kw.pop("name", "Owner Site"),
        # A site with something to tear down in every step, so an ordering test is not
        # quietly passing because every step returned "nothing to do".
        script_name=kw.pop("script_name", "paw-site-x"),
        deploy_target=kw.pop("deploy_target", "wfp"),
        d1_database_id=kw.pop("d1_database_id", "db-1"),
        signed_key=kw.pop("signed_key", "key-abc"),
        subscription_status=kw.pop("subscription_status", "active"),
        billing_rail=kw.pop("billing_rail", "credits"),
        domains=kw.pop(
            "domains",
            [SiteDomain(hostname="www.x.com", cf_hostname_id="ch_1", cf_route_id="rt_1")],
        ),
        **kw,
    )
    await doc.insert()
    return doc


class _ExportStub:
    """Stands in for ``create_site_export``, recording when it ran."""

    def __init__(
        self, *, order: list[str], export_id: str = "exp-1", raises: Exception | None = None
    ):
        self.order = order
        self.export_id = export_id
        self.raises = raises
        self.calls = 0

    async def __call__(self, *, workspace_id: str, user_id: str, site_id: str) -> Any:
        self.calls += 1
        self.order.append("export")
        if self.raises is not None:
            raise self.raises

        class _Row:
            id = self.export_id
            status = "ready"

        return _Row()


@pytest.fixture
def _order() -> list[str]:
    return []


# ── The forced export comes first, and gates everything ──────────────────────


@pytest.mark.asyncio
async def test_the_export_runs_before_any_destructive_step(beanie_test_db, monkeypatch, _order):
    """The whole precondition, in one assertion: 'export' is the FIRST thing that
    happened, ahead of every cascade step."""
    site = await _seed()
    monkeypatch.setattr(sites_service, "create_site_export", _ExportStub(order=_order))

    deps = _Deps()
    original = sites_service.record_delete_progress

    async def _tracking(doc: Any) -> None:
        _order.extend(k for k in doc.delete_ledger if k not in _order)
        await original(doc)

    monkeypatch.setattr(sites_service, "record_delete_progress", _tracking)

    await delete_job_mod.run_site_delete({}, WORKSPACE, str(site.id), _deps=deps)

    assert _order[0] == "export"
    assert _order[1:] == list(CASCADE_STEPS)


@pytest.mark.asyncio
async def test_an_unusable_export_stops_the_delete_before_anything_is_destroyed(
    beanie_test_db, monkeypatch, _order
):
    """THE SAFETY NET. An export that cannot be vouched for must leave the site exactly
    as it was — not 'mostly there', not 'billing already cancelled'. The client's
    sentence for this case promises the user their site is untouched, and this is what
    makes that sentence true."""
    site = await _seed()
    monkeypatch.setattr(
        sites_service,
        "create_site_export",
        _ExportStub(
            order=_order,
            raises=ValidationError(
                "sites.export_unavailable", "This site's live data cannot be read."
            ),
        ),
    )
    deps = _Deps()

    await delete_job_mod.run_site_delete({}, WORKSPACE, str(site.id), _deps=deps)

    row = await Site.get(site.id)
    assert row is not None, "the site must survive an export that failed"
    assert row.delete_status == "failed"
    assert row.delete_reason == "export:sites_export_unavailable"
    assert row.delete_reason.split(":")[0] == "export"
    # Nothing was torn down.
    assert row.delete_ledger == {}
    assert deps.cloudflare.calls == []
    assert deps.assets.purged == []
    assert deps.purged_records == []
    # And the site is still billing, because we never got as far as stopping it.
    assert row.subscription_status == "active"


@pytest.mark.asyncio
async def test_a_bare_ExportUnavailable_is_still_reported_as_the_export_step(
    beanie_test_db, monkeypatch, _order
):
    """``ExportUnavailable`` is a plain ``Exception``, not a ``CloudError`` — the exact
    shape that escaped a CloudError-only handler as a bare 500 once before (#2135). Here
    it must still land as an ``export:`` reason rather than an unclassified stop."""
    site = await _seed()
    monkeypatch.setattr(
        sites_service,
        "create_site_export",
        _ExportStub(order=_order, raises=ExportUnavailable("no D1 here")),
    )

    await delete_job_mod.run_site_delete({}, WORKSPACE, str(site.id), _deps=_Deps())

    row = await Site.get(site.id)
    assert row.delete_status == "failed"
    assert row.delete_reason == "export:exportunavailable"


@pytest.mark.asyncio
async def test_an_export_that_is_not_ready_is_refused(beanie_test_db, monkeypatch, _order):
    """A row that came back in any state but ``ready`` has no bytes. 'We kept a copy' is
    never assumed from the existence of a row."""
    site = await _seed()

    class _NotReady(_ExportStub):
        async def __call__(self, **_kw: Any) -> Any:
            self.order.append("export")

            class _Row:
                id = "exp-1"
                status = "pending"

            return _Row()

    monkeypatch.setattr(sites_service, "create_site_export", _NotReady(order=_order))

    await delete_job_mod.run_site_delete({}, WORKSPACE, str(site.id), _deps=_Deps())

    row = await Site.get(site.id)
    assert row.delete_status == "failed"
    assert row.delete_reason == "export:export_not_ready"
    assert row.delete_ledger == {}


@pytest.mark.asyncio
async def test_the_export_id_is_recorded_before_the_cascade_runs(
    beanie_test_db, monkeypatch, _order
):
    """The export outlives the site, so the pointer to it has to be written while there
    is still a row to write it on."""
    site = await _seed()
    monkeypatch.setattr(
        sites_service, "create_site_export", _ExportStub(order=_order, export_id="exp-42")
    )
    seen: list[str] = []

    class _WatchingCF(_CF):
        def __getattr__(self, name: str) -> Any:
            inner = super().__getattr__(name)

            async def _call(*a: Any, **kw: Any) -> None:
                row = await Site.get(site.id)
                seen.append(row.delete_export_id)
                await inner(*a, **kw)

            return _call

    await delete_job_mod.run_site_delete({}, WORKSPACE, str(site.id), _deps=_Deps(cf=_WatchingCF()))

    assert seen, "the cascade never reached Cloudflare"
    assert all(v == "exp-42" for v in seen)


@pytest.mark.asyncio
async def test_a_resume_reuses_this_attempts_export_rather_than_retaking_it(
    beanie_test_db, monkeypatch, _order
):
    """A ``ready`` export already named on the row was taken by THIS delete, before
    anything was destroyed, so it is a faithful copy and re-taking it would only cost a
    second full read of a database about to be deleted."""
    from pocketpaw_ee.cloud.models.site_export import SiteExport

    export = SiteExport(
        workspace=WORKSPACE, owner=OWNER, site_id="", status="ready", storage_key="k"
    )
    site = await _seed()
    export.site_id = str(site.id)
    await export.insert()
    await site.set({"delete_export_id": str(export.id)})
    site.delete_export_id = str(export.id)

    stub = _ExportStub(order=_order)
    monkeypatch.setattr(sites_service, "create_site_export", stub)

    await delete_job_mod.run_site_delete({}, WORKSPACE, str(site.id), _deps=_Deps())

    assert stub.calls == 0, "a ready export from this attempt should not be retaken"


@pytest.mark.asyncio
async def test_a_failed_export_on_the_row_is_not_reused(beanie_test_db, monkeypatch, _order):
    """Only ``ready`` satisfies the gate. A failed export row is the record of an
    attempt, not a copy of anyone's data."""
    from pocketpaw_ee.cloud.models.site_export import SiteExport

    site = await _seed()
    export = SiteExport(
        workspace=WORKSPACE, owner=OWNER, site_id=str(site.id), status="failed", error="nope"
    )
    await export.insert()
    await site.set({"delete_export_id": str(export.id)})
    site.delete_export_id = str(export.id)

    stub = _ExportStub(order=_order, export_id="exp-fresh")
    monkeypatch.setattr(sites_service, "create_site_export", stub)

    await delete_job_mod.run_site_delete({}, WORKSPACE, str(site.id), _deps=_Deps())

    assert stub.calls == 1


# ── The cascade's own effects have to reach the database ─────────────────────


@pytest.mark.asyncio
async def test_a_stopped_cascade_persists_the_billing_stop_it_already_made(
    beanie_test_db, monkeypatch, _order
):
    """THE LEDGER IS NOT THE ONLY THING A STEP CHANGES.

    ``_stop_billing`` ends the charging by clearing ``subscription_status`` in memory —
    on the credits rail that local write IS the mechanism, because the renewal sweeper
    selects on exactly that field. If the save callback wrote only ``delete_ledger``, a
    cascade that stopped later would leave a row whose ledger says billing is done and
    whose status still says ``active``; the resume would then SKIP the billing step on
    the strength of that ledger and the customer would keep paying for a site that no
    longer serves.
    """
    site = await _seed()
    monkeypatch.setattr(sites_service, "create_site_export", _ExportStub(order=_order))

    # Stop the cascade at the Worker delete — after billing and the key revoke.
    deps = _Deps(cf=_CF(fail_on="delete_worker"))

    await delete_job_mod.run_site_delete({}, WORKSPACE, str(site.id), _deps=deps)

    row = await Site.get(site.id)
    assert row is not None
    assert row.delete_status == "failed"
    assert row.delete_reason == "script:runtimeerror"
    # The ledger recorded the two steps that ran...
    assert row.delete_ledger.get("billing") == "done"
    assert row.delete_ledger.get("revoke") == "done"
    # ...and so did the FIELDS those steps changed.
    assert row.subscription_status == "none", "the billing stop never reached the database"
    assert row.renewal_date is None
    assert row.signed_key == "", "the public ingest key was not actually revoked"
    assert row.revoked is True


@pytest.mark.asyncio
async def test_a_stopped_cascade_leaves_the_site_and_its_ledger(
    beanie_test_db, monkeypatch, _order
):
    """The row survives a failure on purpose: it carries the ledger, which is the only
    thing that knows which steps finished."""
    site = await _seed()
    monkeypatch.setattr(sites_service, "create_site_export", _ExportStub(order=_order))

    await delete_job_mod.run_site_delete(
        {}, WORKSPACE, str(site.id), _deps=_Deps(cf=_CF(fail_on="delete_database"))
    )

    row = await Site.get(site.id)
    assert row is not None
    assert row.delete_status == "failed"
    assert row.delete_reason.startswith("d1:")
    assert "script" in row.delete_ledger


@pytest.mark.asyncio
async def test_a_cascade_failure_never_raises_out_of_the_job(beanie_test_db, monkeypatch, _order):
    """The row settles at ``failed`` carrying the step. Raising as well would add an
    arq-level error beside a row that already says what happened — and with
    ``max_tries=1`` nothing would act on it anyway."""
    site = await _seed()
    monkeypatch.setattr(sites_service, "create_site_export", _ExportStub(order=_order))

    # No exception expected.
    await delete_job_mod.run_site_delete(
        {}, WORKSPACE, str(site.id), _deps=_Deps(cf=_CF(fail_on="delete_worker_route"))
    )
    assert (await Site.get(site.id)).delete_status == "failed"


# ── Completion ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_completed_delete_removes_the_site_document(beanie_test_db, monkeypatch, _order):
    """The row going away IS the success signal — there is no terminal status, because
    this is the document the status would live on."""
    site = await _seed()
    monkeypatch.setattr(sites_service, "create_site_export", _ExportStub(order=_order))
    deps = _Deps()

    await delete_job_mod.run_site_delete({}, WORKSPACE, str(site.id), _deps=deps)

    assert await Site.get(site.id) is None
    assert deps.purged_records == [(WORKSPACE, str(site.id))]


@pytest.mark.asyncio
async def test_the_job_walks_the_row_through_the_phases_the_client_renders(
    beanie_test_db, monkeypatch, _order
):
    """``exporting`` then ``tearing_down``. The client turns these into the only
    question a site's owner actually has — has my data been saved yet — so a job that
    skipped straight to tearing down would answer it wrongly."""
    site = await _seed()
    phases: list[str] = []

    async def _record_phase(**kw: Any) -> Any:
        row = await Site.get(site.id)
        phases.append(row.delete_status)

        class _Row:
            id = "exp-1"
            status = "ready"

        return _Row()

    monkeypatch.setattr(sites_service, "create_site_export", _record_phase)

    class _PhaseCF(_CF):
        def __getattr__(self, name: str) -> Any:
            inner = super().__getattr__(name)

            async def _call(*a: Any, **kw: Any) -> None:
                row = await Site.get(site.id)
                if row.delete_status not in phases:
                    phases.append(row.delete_status)
                await inner(*a, **kw)

            return _call

    await delete_job_mod.run_site_delete({}, WORKSPACE, str(site.id), _deps=_Deps(cf=_PhaseCF()))

    assert phases == ["exporting", "tearing_down"]


@pytest.mark.asyncio
async def test_a_site_that_is_already_gone_is_a_no_op(beanie_test_db, monkeypatch, _order):
    """A job that wakes to find no row has found its own earlier attempt's success."""
    stub = _ExportStub(order=_order)
    monkeypatch.setattr(sites_service, "create_site_export", stub)

    await delete_job_mod.run_site_delete({}, WORKSPACE, "68bf0000000000000000dead", _deps=_Deps())

    assert stub.calls == 0


@pytest.mark.asyncio
async def test_the_job_cannot_delete_a_site_from_another_workspace(
    beanie_test_db, monkeypatch, _order
):
    """The workspace rides in the payload and the read is scoped to it, so a bad payload
    cannot reach another tenant's row."""
    site = await _seed(workspace="ws_other")
    stub = _ExportStub(order=_order)
    monkeypatch.setattr(sites_service, "create_site_export", stub)

    await delete_job_mod.run_site_delete({}, WORKSPACE, str(site.id), _deps=_Deps())

    assert stub.calls == 0
    assert await Site.get(site.id) is not None


# ── The dependent rows ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_purging_records_takes_this_sites_rows_and_only_this_sites(beanie_test_db):
    """Tenancy and site scope are both IN the query. A purge that filtered afterwards
    would already have read the other site's submissions."""
    mine, theirs = "site-a", "site-b"
    for site_id, workspace in ((mine, WORKSPACE), (theirs, WORKSPACE), (mine, "ws_other")):
        await Lead(
            workspace=workspace,
            site_id=site_id,
            form_type="contact",
            properties={},
            source=LeadSource(form_type="contact", site_id=site_id),
        ).insert()

    removed = await sites_service.purge_site_records(workspace_id=WORKSPACE, site_id=mine)

    assert removed == 1
    left = [(d.workspace, d.site_id) for d in await Lead.find_all().to_list()]
    assert sorted(left) == sorted([(WORKSPACE, theirs), ("ws_other", mine)])


@pytest.mark.asyncio
async def test_purging_records_takes_both_rate_counter_scopes(beanie_test_db):
    """``scope_id`` is the site id for the overall scope and ``"{site_id}:{rate_key}"``
    for the per-IP one — one anchored prefix has to catch both, and nothing else."""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    for scope, scope_id in (
        ("site", "site-a"),
        ("ip", "site-a:1.2.3.4"),
        ("site", "site-abc"),  # a DIFFERENT site whose id merely starts the same way
        ("site", "site-b"),
    ):
        await SiteRateCounter(scope=scope, scope_id=scope_id, bucket=now, hits=1).insert()

    await sites_service.purge_site_records(workspace_id=WORKSPACE, site_id="site-a")

    left = sorted(d.scope_id for d in await SiteRateCounter.find_all().to_list())
    assert left == ["site-abc", "site-b"]


# ── The enqueue ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_failed_enqueue_does_not_pin_the_row_in_queued(beanie_test_db):
    """A row left in ``queued`` behind a job that never existed would refuse every
    future delete of this site until the staleness window lapsed."""

    class _DeadPool:
        async def enqueue_job(self, *_a: Any, **_kw: Any) -> None:
            raise RuntimeError("redis is gone")

    site = await _seed()

    with pytest.raises(RuntimeError):
        await delete_job_mod.enqueue_site_delete(site, _pool_override=_DeadPool())

    row = await Site.get(site.id)
    assert row.delete_status == "failed"
    assert row.delete_reason == "enqueue:pool_or_enqueue_raised"


@pytest.mark.asyncio
async def test_arq_refusing_the_id_is_treated_as_a_failed_enqueue(beanie_test_db):
    """arq answers ``None`` for a duplicate id. Reading that as 'a job is coming' is how
    a row sits in ``queued`` forever."""

    class _RefusingPool:
        async def enqueue_job(self, *_a: Any, **_kw: Any) -> None:
            return None

    site = await _seed()

    with pytest.raises(RuntimeError):
        await delete_job_mod.enqueue_site_delete(site, _pool_override=_RefusingPool())

    assert (await Site.get(site.id)).delete_status == "failed"


@pytest.mark.asyncio
async def test_the_job_id_is_not_deterministic(beanie_test_db):
    """A stable id would make arq refuse the RETRY of a failed delete for as long as its
    result lived in Redis — a single-flight guard nobody asked for, in the wrong layer,
    and invisible because the refusal is a ``None`` return."""
    a = delete_job_mod._mint_job_id("s1")
    b = delete_job_mod._mint_job_id("s1")
    assert a != b
    assert a.startswith("site-delete-s1-")


@pytest.mark.asyncio
async def test_the_delete_is_registered_on_the_lane_that_consumes_the_queue(monkeypatch):
    """A job enqueued to a queue no worker reads is a job that never runs. This asserts
    the registration is on the WINNING ``functions`` list — a second assignment in that
    class would silently override the first."""
    monkeypatch.setenv("POCKETPAW_REDIS_URL", "redis://localhost:6379")
    import importlib

    from pocketpaw_ee.sites import build_worker

    importlib.reload(build_worker)
    names = [getattr(f, "name", None) for f in build_worker.WorkerSettings.functions]
    assert delete_job_mod.SITE_DELETE_FUNCTION_NAME in names
    assert build_worker.WorkerSettings.queue_name == delete_job_mod.SITE_BUILD_QUEUE_NAME
    assert build_worker.WorkerSettings.max_tries == 1


@pytest.mark.asyncio
async def test_a_cascade_step_failure_carries_the_step_in_the_reason() -> None:
    """The client splits ``delete_reason`` on the colon and renders the STEP half, so
    the shape is a wire contract rather than a log convention."""
    err = CascadeStepFailed("hostnames", "timeout_error")
    assert err.reason == "hostnames:timeout_error"
    assert err.reason.split(":")[0] == "hostnames"
