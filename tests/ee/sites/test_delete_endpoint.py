# tests/ee/sites/test_delete_endpoint.py — the HTTP contract of the site-delete lane
# (sites lifecycle wave 1, feat/sites-delete-endpoint).
#
# WHAT IS UNDER TEST IS A CONTRACT SOMEONE ELSE ALREADY SHIPPED AGAINST. The delete UI
# (paw-enterprise #895) went out before this backend existed, reading
# ``core/sites/delete-api.ts`` and ``delete-types.ts``. So these tests pin the wire, not
# the implementation: the 202 and its body, the 403-vs-404 split, and — the one that
# looks like a bug if you do not know the design — that the poll 404s ON SUCCESS.
#
# The 404-on-success case gets its own test because it is the single assumption most
# likely to be "fixed" by a future reader into a terminal ``deleted`` status. It cannot
# be: step 8 of the cascade deletes the Site document, and ``delete_status`` is a field
# ON that document. A terminal status would require keeping the row the delete exists to
# remove, and the shipped client reads the 404 as completion (``isDeleteFinished``).
#
# Auth wiring mirrors test_router.py — see the long note in ``_build_app`` there about
# why this module does NOT patch ``get_workspace_plan`` itself.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.sites import delete_job as delete_job_mod
from pocketpaw_ee.sites import service as sites_service

WORKSPACE = "ws_owner"
OWNER = "user-test-1"


class _FakeMembership:
    def __init__(self, workspace: str, role: str = "member") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    def __init__(self, workspace_id: str, user_id: str = OWNER) -> None:
        self.id = user_id
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace=workspace_id)]


def _build_app(workspace_id: str, user_id: str = OWNER) -> FastAPI:
    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.sites.router import router as sites_router

    fake_user = _FakeUser(workspace_id, user_id)
    app = FastAPI()
    add_error_handler(app)
    app.include_router(sites_router, prefix="/api/v1")

    async def _ctx() -> RequestContext:
        return RequestContext(
            user_id=str(fake_user.id),
            workspace_id=workspace_id,
            request_id="test",
            scope=ScopeKind.WORKSPACE,
            started_at=datetime.now(UTC),
        )

    app.dependency_overrides[request_context] = _ctx
    app.dependency_overrides[current_active_user] = lambda: fake_user
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id
    app.dependency_overrides[require_license] = lambda: None
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seed(**kw: Any) -> Site:
    """A Site row with only what a delete needs. Inserted directly rather than
    published, because these tests are about the delete gate and ``publish`` would drag
    a generator, a bundle reader and a Cloudflare double in with it."""
    doc = Site(
        workspace=kw.pop("workspace", WORKSPACE),
        pocket_id=kw.pop("pocket_id", "pk1"),
        owner=kw.pop("owner", OWNER),
        name=kw.pop("name", "Owner Site"),
        **kw,
    )
    await doc.insert()
    return doc


@pytest_asyncio.fixture
async def _no_real_enqueue(monkeypatch) -> list[Any]:
    """Record enqueues instead of reaching Redis, and still perform the CLAIM.

    The claim is left real on purpose: it is the conditional write that decides whether
    a second delete of one site is refused, and a double that skipped it would make the
    in-flight test pass for the wrong reason.
    """
    calls: list[Any] = []

    async def _fake(site: Any, **_kw: Any) -> str | None:
        claimed = await sites_service.claim_delete_queued(site, job_id="job-1", timeout_seconds=900)
        if not claimed:
            return None
        calls.append(str(site.id))
        return "job-1"

    monkeypatch.setattr(delete_job_mod, "enqueue_site_delete", _fake)
    return calls


# ── DELETE /sites/{site_id} ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_answers_202_with_the_job_handle(beanie_test_db, _no_real_enqueue):
    """202, not 204. The site is still serving when this returns — the teardown is a
    job — and the body carries what the client needs to start polling."""
    site = await _seed()
    app = _build_app(WORKSPACE)

    async with _client(app) as c:
        res = await c.delete(f"/api/v1/sites/{site.id}")

    assert res.status_code == 202
    body = res.json()
    assert body["site_id"] == str(site.id)
    assert body["status"] == "queued"
    assert body["job_id"] == "job-1"
    assert _no_real_enqueue == [str(site.id)]


@pytest.mark.asyncio
async def test_delete_stamps_the_row_so_a_reload_can_find_the_attempt(
    beanie_test_db, _no_real_enqueue
):
    """``delete_job_id`` is PERSISTED, not transient. A queued destructive job is
    exactly when someone reloads the page, and a handle that lived only in the response
    would be gone at the moment the wait is longest."""
    site = await _seed()
    app = _build_app(WORKSPACE)

    async with _client(app) as c:
        await c.delete(f"/api/v1/sites/{site.id}")

    row = await Site.get(site.id)
    assert row.delete_status == "queued"
    assert row.delete_job_id == "job-1"
    assert row.delete_started_at is not None


@pytest.mark.asyncio
async def test_a_non_owner_in_the_workspace_gets_403_and_not_404(beanie_test_db, _no_real_enqueue):
    """403 and 404 are two different answers on purpose.

    A member who can SEE the site but may not destroy it must be told the rule, not
    told the site does not exist — the client renders a permission sentence off exactly
    this status. Nothing may be enqueued.
    """
    site = await _seed(owner="somebody-else")
    app = _build_app(WORKSPACE)

    async with _client(app) as c:
        res = await c.delete(f"/api/v1/sites/{site.id}")

    assert res.status_code == 403
    assert res.json()["error"]["code"] == "site.not_owner"
    assert _no_real_enqueue == []
    row = await Site.get(site.id)
    assert row.delete_status == "none"


@pytest.mark.asyncio
async def test_a_site_in_another_workspace_is_404_not_403(beanie_test_db, _no_real_enqueue):
    """Cross-tenant is a 404 even though the caller is not the owner either. Confirming
    a stranger's site exists is itself the leak, so the tenancy answer wins over the
    ownership one."""
    site = await _seed(workspace="ws_other", owner="someone-there")
    app = _build_app(WORKSPACE)

    async with _client(app) as c:
        res = await c.delete(f"/api/v1/sites/{site.id}")

    assert res.status_code == 404
    assert _no_real_enqueue == []


@pytest.mark.asyncio
async def test_a_second_delete_reports_the_attempt_already_running(
    beanie_test_db, _no_real_enqueue
):
    """Two deletes of one site must not become two cascades. The loser reports the
    in-flight attempt rather than a conflict the user cannot act on — the site IS being
    deleted, which is what they asked for."""
    site = await _seed()
    app = _build_app(WORKSPACE)

    async with _client(app) as c:
        first = await c.delete(f"/api/v1/sites/{site.id}")
        second = await c.delete(f"/api/v1/sites/{site.id}")

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["status"] == "queued"
    # One claim won, so exactly one job was ever queued.
    assert _no_real_enqueue == [str(site.id)]


@pytest.mark.asyncio
async def test_a_failed_delete_can_be_started_again(beanie_test_db, _no_real_enqueue):
    """``failed`` is deliberately NOT an in-flight status. The cascade resumes from its
    ledger, so a site whose teardown stopped must be re-deletable — otherwise one bad
    afternoon at Cloudflare leaves a site nobody can ever remove."""
    site = await _seed(delete_status="failed", delete_reason="script:runtime_error")
    app = _build_app(WORKSPACE)

    async with _client(app) as c:
        res = await c.delete(f"/api/v1/sites/{site.id}")

    assert res.status_code == 202
    assert _no_real_enqueue == [str(site.id)]
    row = await Site.get(site.id)
    # The reason is cleared so a retry never shows the previous attempt's step...
    assert row.delete_reason is None


@pytest.mark.asyncio
async def test_retrying_a_failed_delete_keeps_the_ledger(beanie_test_db, _no_real_enqueue):
    """...and the LEDGER survives, because it is what makes the retry a resume.

    Clearing it alongside the reason would re-run every destructive step the first
    attempt finished: re-cancel the billing, re-purge the bucket.
    """
    site = await _seed(
        delete_status="failed",
        delete_reason="script:runtime_error",
        delete_ledger={"billing": "done", "revoke": "done"},
    )
    app = _build_app(WORKSPACE)

    async with _client(app) as c:
        await c.delete(f"/api/v1/sites/{site.id}")

    row = await Site.get(site.id)
    assert row.delete_ledger == {"billing": "done", "revoke": "done"}


# ── GET /sites/{site_id}/delete-status ───────────────────────────────────────


@pytest.mark.asyncio
async def test_the_poll_reports_the_phase_and_the_reason(beanie_test_db):
    site = await _seed(
        delete_status="tearing_down",
        delete_reason=None,
        delete_ledger={"billing": "done"},
        delete_export_id="exp-1",
        delete_job_id="job-9",
    )
    app = _build_app(WORKSPACE)

    async with _client(app) as c:
        res = await c.get(f"/api/v1/sites/{site.id}/delete-status")

    assert res.status_code == 200
    body = res.json()
    assert body["site_id"] == str(site.id)
    assert body["delete_status"] == "tearing_down"
    assert body["delete_reason"] is None
    assert body["delete_ledger"] == {"billing": "done"}
    assert body["delete_export_id"] == "exp-1"
    assert body["delete_job_id"] == "job-9"


@pytest.mark.asyncio
async def test_a_stopped_delete_reports_the_step_that_stopped_it(beanie_test_db):
    """``delete_reason`` is the raw ``"<step>:<cause>"``. The client splits on the colon
    and renders the STEP, so the step half has to survive the wire verbatim."""
    site = await _seed(delete_status="failed", delete_reason="export:export_unavailable")
    app = _build_app(WORKSPACE)

    async with _client(app) as c:
        res = await c.get(f"/api/v1/sites/{site.id}/delete-status")

    assert res.json()["delete_reason"] == "export:export_unavailable"
    assert res.json()["delete_reason"].split(":")[0] == "export"


@pytest.mark.asyncio
async def test_the_poll_404s_once_the_delete_has_FINISHED(beanie_test_db):
    """THE 404 IS THE SUCCESS SIGNAL, not an error.

    The cascade's last step deletes the Site document, and ``delete_status`` lives on
    that document — so a finished delete has nothing left to report a status on. The
    shipped client's ``isDeleteFinished`` reads exactly this 404 as completion. A
    terminal ``deleted`` status added here would mean keeping the row the delete exists
    to remove.
    """
    site = await _seed(delete_status="tearing_down")
    app = _build_app(WORKSPACE)

    async with _client(app) as c:
        before = await c.get(f"/api/v1/sites/{site.id}/delete-status")
        assert before.status_code == 200

        # What the cascade's final step does.
        await sites_service.delete_site_document(site)

        after = await c.get(f"/api/v1/sites/{site.id}/delete-status")

    assert after.status_code == 404


@pytest.mark.asyncio
async def test_no_delete_status_is_ever_terminal_success(beanie_test_db):
    """A guard against the most likely future 'fix'. If someone adds a terminal status,
    the poll stops 404-ing on success and every completed delete reads as a failure to
    the shipped client."""
    from pocketpaw_ee.sites.dto import SiteDeleteStatusResponse

    assert SiteDeleteStatusResponse.model_fields["delete_status"].default == "none"
    # The in-flight set is the whole running vocabulary; nothing in it means "done".
    assert sites_service.DELETE_IN_FLIGHT_STATUSES == frozenset(
        {"queued", "exporting", "tearing_down"}
    )
    assert "deleted" not in sites_service.DELETE_IN_FLIGHT_STATUSES


@pytest.mark.asyncio
async def test_the_poll_is_owner_only_too(beanie_test_db):
    """``delete_reason`` names which step of a teardown failed — operational detail
    about a site the reader may see but not administer."""
    site = await _seed(owner="somebody-else", delete_status="tearing_down")
    app = _build_app(WORKSPACE)

    async with _client(app) as c:
        res = await c.get(f"/api/v1/sites/{site.id}/delete-status")

    assert res.status_code == 403
    assert res.json()["error"]["code"] == "site.not_owner"


@pytest.mark.asyncio
async def test_the_poll_is_tenant_scoped(beanie_test_db):
    site = await _seed(workspace="ws_other", owner="someone-there")
    app = _build_app(WORKSPACE)

    async with _client(app) as c:
        res = await c.get(f"/api/v1/sites/{site.id}/delete-status")

    assert res.status_code == 404


@pytest.mark.asyncio
async def test_a_malformed_site_id_is_404_and_not_a_500(beanie_test_db, _no_real_enqueue):
    """A caller-supplied id that is not an ObjectId means "no such site", not an
    unhandled ``InvalidId`` — the same guard ``_load`` already applies everywhere else."""
    app = _build_app(WORKSPACE)

    async with _client(app) as c:
        assert (await c.delete("/api/v1/sites/not-an-objectid")).status_code == 404
        assert (await c.get("/api/v1/sites/not-an-objectid/delete-status")).status_code == 404
