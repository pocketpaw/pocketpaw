# tests/ee/sites/test_project_download_endpoint.py — proves the gate on
# GET /sites/{site_id}/project: who may take a Paw Site's project away as an archive,
# and what the refusal says.
#
# WHY THIS FILE EXISTS AT ALL. ``project_zip`` assembles the archive and checks
# NOTHING about entitlement — it reads the pocket through ``pockets_service.get``, the
# deliberately ungated pipeline reader that hands back the real source map whatever
# the plan says, because the build lane needs it to. Its own docstring says it "adds
# no isolation rules of its own", meaning tenancy only. So the endpoint is the entire
# paywall, and deleting one line in it does not degrade the feature, it gives the
# feature away. Every test here exists to make that line's removal loud.
#
# WHAT A REVIEWER SHOULD CHECK, in order:
#   (a) the grant — a paid site with a live subscription gets bytes; a floor site and
#       a LAPSED paid site are both 402 and they get DIFFERENT remedies, because
#       "upgrade" and "renew" are different instructions to different customers;
#   (b) BOTH ARMS OF ``sites_enforced()`` — a self-hosted deployment has no billing
#       and must not acquire a paywall on its own source code. Without the
#       not-enforced case, deleting the early return escapes every other test here;
#   (c) the honest empty — a Ripple site is a 400, never a zero-byte zip, since an
#       empty archive is indistinguishable from a site whose files vanished;
#   (d) the order — tenancy is resolved BEFORE the entitlement, so a 402 can never
#       confirm that another workspace's site exists;
#   (e) the error surface — the ``ProjectZipError`` family is not in the CloudError
#       hierarchy, so an unmapped one leaves an unhandled 500 that arrives in a
#       browser stripped of its CORS headers and reads as a CORS misconfiguration
#       rather than as a failed download;
#   (f) the pre-check — ``project_download`` on the per-site entitlements read, which
#       is what lets the button disable itself instead of provoking the 402.
#
# Driven over the real router through ``add_error_handler``, so every status code and
# error code asserted here is the one a client actually receives rather than the
# exception the service raised. Beanie in this tree is session-scoped over mongomock,
# so every test mints its own workspace and pocket ids instead of sharing them.
from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud.models.pocket import Pocket
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.sites import project_zip
from pocketpaw_ee.sites import service as sites_service

pytestmark = pytest.mark.asyncio

OWNER = "user_pd_1"

SOURCE_MAP: dict[str, Any] = {
    "src/routes/+page.svelte": "<script>let n = 1;</script>\n<h1>Invoices, paid faster</h1>\n",
    "src/routes/+page.ts": "export const prerender = true;\n",
    "src/app.css": ":root { --ink: #17130f; }\n",
    "src/lib/components/Hero.svelte": "<section>hero</section>\n",
}


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
    """The sites router with auth waived and the CloudError handler mounted.

    ``add_error_handler`` is the point: without it a raised ``CloudError`` surfaces as
    an exception rather than a response, and a test asserting "402" would be asserting
    on the wrong layer — the thing under test is what the client receives.
    """
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


async def _seed_site(
    *,
    workspace: str,
    plan_tier: str | None,
    subscription_status: str = "active",
    engine: str = "svelte",
    source: dict[str, Any] | None = None,
) -> Site:
    """A pocket holding the authored source, and the Site row that points at it.

    Both inserted directly rather than published: this file is about the download
    gate, and ``publish_pocket`` would drag a generator, a bundle reader and a
    Cloudflare double in with it — the same reasoning ``test_delete_endpoint`` gives
    for seeding its own rows.

    ``visibility="workspace"`` because the assembler reads the pocket through
    ``pockets_service.get``, whose read gate refuses only ``private``.
    """
    pocket = Pocket(
        workspace=workspace,
        name="Invoice site",
        type="site",
        owner=OWNER,
        visibility="workspace",
        engine=engine,
        source=dict(SOURCE_MAP) if source is None else source,
        widgets=[],
    )
    await pocket.insert()

    site = Site(
        workspace=workspace,
        pocket_id=str(pocket.id),
        owner=OWNER,
        name="Invoice site",
        plan_tier=plan_tier,
        subscription_status=subscription_status,
    )
    await site.insert()
    return site


@pytest.fixture
def enforced(monkeypatch):
    """Turn the sites paywall on or off.

    Patched at ``billing.enforcement`` and not on the sites module, because the gate
    imports it lazily inside the function — the attribute is resolved at call time, so
    the module-level name is the one a test must replace.
    """

    def _set(on: bool) -> None:
        monkeypatch.setattr("pocketpaw_ee.cloud.billing.enforcement.sites_enforced", lambda: on)

    _set(True)
    return _set


# ---------------------------------------------------------------------------
# (a) The grant, and the two refusals that are not the same sentence.
# ---------------------------------------------------------------------------


async def test_a_paid_site_downloads_its_project(enforced) -> None:
    """A site on a paid rung with a live subscription gets the archive, as a zip
    attachment, containing the files its owner authored."""
    site = await _seed_site(workspace="ws_pd_paid", plan_tier="site")

    app = _build_app("ws_pd_paid")
    async with _client(app) as client:
        resp = await client.get(f"/api/v1/sites/{site.id}/project")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert resp.headers["content-disposition"].startswith("attachment; filename=")
    assert resp.headers["content-length"] == str(len(resp.content))

    with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
        names = set(archive.namelist())
        # The authored files are in there, and so is the build shell that makes them
        # install — proving this is the assembled project and not a dump of `source`.
        assert "src/routes/+page.svelte" in names
        assert "package.json" in names
        assert b"Invoices, paid faster" in archive.read("src/routes/+page.svelte")


async def test_a_floor_site_is_refused_and_told_to_upgrade(enforced) -> None:
    """The free rung does not sell the download. 402 with the per-SITE code, so the UI
    prompts a site upgrade rather than a workspace one."""
    site = await _seed_site(workspace="ws_pd_floor", plan_tier="free")

    app = _build_app("ws_pd_floor")
    async with _client(app) as client:
        resp = await client.get(f"/api/v1/sites/{site.id}/project")

    assert resp.status_code == 402
    body = resp.json()
    assert body["error"]["code"] == "billing.project_download_not_entitled"
    assert "upgrade" in body["error"]["message"].lower()


async def test_a_lapsed_paid_site_is_refused_and_told_to_renew(enforced) -> None:
    """The refusal a paid customer gets, and it must NOT be the floor site's sentence.

    Cancellation never resets ``plan_tier``, so reading the tier alone would hand this
    site a free download. It keeps the tier it chose and loses the capability, and the
    remedy is to fix the payment rather than to buy a bigger plan.
    """
    site = await _seed_site(
        workspace="ws_pd_lapsed", plan_tier="site", subscription_status="cancelled"
    )

    app = _build_app("ws_pd_lapsed")
    async with _client(app) as client:
        resp = await client.get(f"/api/v1/sites/{site.id}/project")

    assert resp.status_code == 402
    message = resp.json()["error"]["message"].lower()
    assert "renew" in message
    assert "upgrade" not in message


# ---------------------------------------------------------------------------
# (b) Both arms of sites_enforced(). Without this test, deleting the early
#     return in the gate escapes every other test in this file.
# ---------------------------------------------------------------------------


async def test_b_self_hosted_download_is_not_paywalled(enforced) -> None:
    """With billing not enforced, a floor site downloads. OSS and self-hosted installs
    have no billing at all, and a self-hoster who could not download their own source
    would be right to call it a bug — the early return is the feature, not a guard."""
    enforced(False)
    site = await _seed_site(workspace="ws_pd_selfhost", plan_tier="free")

    app = _build_app("ws_pd_selfhost")
    async with _client(app) as client:
        resp = await client.get(f"/api/v1/sites/{site.id}/project")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"


# ---------------------------------------------------------------------------
# (c) A ripple site has nothing to give, and says so.
# ---------------------------------------------------------------------------


async def test_c_a_ripple_site_is_a_400_not_an_empty_zip(enforced) -> None:
    """A Ripple site keeps a spec, not files. The refusal is explicit because a
    zero-byte archive cannot be told apart from a site whose files disappeared, and
    the archive is the one artifact a customer cannot re-derive.

    THE PAID TIER HERE IS DELIBERATE. On ``free`` this test would 402 at the
    entitlement and never reach the engine check, so it would pass while proving
    nothing about the 400. Do not simplify it to the floor.
    """
    site = await _seed_site(
        workspace="ws_pd_ripple", plan_tier="staff", engine="ripple", source=None
    )

    app = _build_app("ws_pd_ripple")
    async with _client(app) as client:
        resp = await client.get(f"/api/v1/sites/{site.id}/project")

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "sites.project_not_downloadable"


# ---------------------------------------------------------------------------
# (d) Tenancy first — a 402 must not confirm that somebody else's site exists.
# ---------------------------------------------------------------------------


async def test_d_another_workspaces_site_is_a_404_not_a_402(enforced) -> None:
    """The order of the two checks is itself the contract. This site is on the FLOOR,
    so if the entitlement ran first the answer would be 402 — which would tell the
    caller the id is real and even leak which plan it is on."""
    site = await _seed_site(workspace="ws_pd_theirs", plan_tier="free")

    app = _build_app("ws_pd_mine")
    async with _client(app) as client:
        resp = await client.get(f"/api/v1/sites/{site.id}/project")

    assert resp.status_code == 404


async def test_d_a_malformed_site_id_is_a_404_not_a_500(enforced) -> None:
    """``_load`` guards the ObjectId cast, so a junk id is "no such site" rather than
    a bson InvalidId escaping as an unhandled 500."""
    app = _build_app("ws_pd_malformed")
    async with _client(app) as client:
        resp = await client.get("/api/v1/sites/not-an-object-id/project")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# (e) The assembler's own failures must arrive as JSON, not as an unhandled raise.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raised", "expected_code"),
    [
        (project_zip.ProjectZipError("broken"), "sites.project_unavailable"),
        (project_zip.ProjectZipTooLarge("too big"), "sites.project_too_large"),
        (project_zip.UnsafeSourcePath("../escape"), "sites.project_unsafe_path"),
    ],
)
async def test_e_assembler_failures_become_mapped_cloud_errors(
    enforced, monkeypatch, raised, expected_code
) -> None:
    """Each member of the ``ProjectZipError`` family is translated.

    None of them is a ``CloudError``, so an unmapped one would leave FastAPI to return
    an unhandled 500 — which on a cross-origin call reaches the browser without its
    CORS headers and gets reported as a CORS misconfiguration, sending whoever
    investigates to the wrong layer entirely.

    They are 500s and not 4xx on purpose: a source map too large for BSON to have
    stored it, a stored path that escapes its root, and a source engine holding no
    source map all mean our own data broke an invariant. Blaming the request would be
    a second wrong signpost.
    """
    site = await _seed_site(workspace=f"ws_pd_err_{expected_code}", plan_tier="staff")

    async def _boom(**_kwargs: Any) -> None:
        raise raised

    monkeypatch.setattr(sites_service.project_zip, "build_project_zip", _boom)

    app = _build_app(f"ws_pd_err_{expected_code}")
    async with _client(app) as client:
        resp = await client.get(f"/api/v1/sites/{site.id}/project")

    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == expected_code


# ---------------------------------------------------------------------------
# (f) The pre-check, so the button can disable itself.
# ---------------------------------------------------------------------------


async def test_f_entitlements_read_publishes_the_download_capability(enforced) -> None:
    """``GET /sites/{id}/entitlements`` carries ``project_download``.

    Without it the only way a client could discover the refusal is to provoke it,
    which is the exact failure ``SiteEntitlementsResponse`` was added to end. Asserted
    in both directions so the field tracks the plan rather than being hardcoded.
    """
    paid = await _seed_site(workspace="ws_pd_ent", plan_tier="staff")
    floor = await _seed_site(workspace="ws_pd_ent", plan_tier="free")

    app = _build_app("ws_pd_ent")
    async with _client(app) as client:
        paid_body = (await client.get(f"/api/v1/sites/{paid.id}/entitlements")).json()
        floor_body = (await client.get(f"/api/v1/sites/{floor.id}/entitlements")).json()

    assert paid_body["project_download"] is True
    assert floor_body["project_download"] is False


async def test_f_the_precheck_agrees_with_the_endpoint(enforced) -> None:
    """The pre-check and the gate must not drift. Same site, both questions: a client
    that trusts the flag and is then refused would be the bug this pins."""
    site = await _seed_site(workspace="ws_pd_agree", plan_tier="free")

    app = _build_app("ws_pd_agree")
    async with _client(app) as client:
        flag = (await client.get(f"/api/v1/sites/{site.id}/entitlements")).json()[
            "project_download"
        ]
        download = await client.get(f"/api/v1/sites/{site.id}/project")

    assert flag is False
    assert download.status_code == 402
