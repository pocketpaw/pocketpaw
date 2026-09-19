# tests/ee/sites/test_foreign_concierge_endpoints.py — the HTTP surface of the
# foreign concierge: bind, read, rotate, rebind.
#
# WHY THIS EXISTS. Four slices built the feature and none of them reached the
# wire: ``bind_foreign_concierge`` lived only in the service, no ``@router``
# decorator reached it, and a frontend building the setup panel had nothing real
# to call. Every assertion here drives an HTTP request, because the properties
# that matter are properties of the ROUTE — a service that dedupes perfectly is
# still a double charge if the endpoint above it calls the always-mints
# primitive, and the service suite cannot see that.
#
# THE THREE THINGS THIS FILE IS FOR:
#
#   * THE MONEY. A first bind charges once; a second charges nothing. Asserted on
#     the WALLET and the COLLECTION rather than on a return value, since a route
#     that re-charges still answers with exactly the right concierge.
#   * THE GATES, AND THAT THEY STAY DISTINGUISHABLE. Unverified and stale are
#     both 403 and they MASK EACH OTHER: delete the unverified arm and the
#     freshness arm still refuses, because ``verification_is_fresh(None)`` is
#     False — so a test asserting only the status code passes against a deleted
#     gate. Every refusal here asserts the CODE.
#   * THE AUTHORIZATION. A first bind commits the workspace to $19/month, so it
#     needs ``sites.buy_plan`` (ADMIN) and not only ``fabric.write``.
#
# EVERY "NOTHING HAPPENED" TEST DRIVES A CONTROL FIRST. A refusal test that
# asserts no row and no debit is indistinguishable from a test whose request
# never reached the endpoint, so each one binds a control pocket in the SAME
# workspace and proves the row count and the balance DO move before asserting
# that the refused call leaves them where the control left them.
#
# tests/mutations/foreign_concierge_endpoints.json is the other half of this file.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud.billing import site_plans
from pocketpaw_ee.cloud.credits import service as credits_service
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.cloud.models.site_origin_claim import SiteOriginClaim

from pocketpaw.paw_bar.store import PawBarStore

_HOST = "brewco.example"
_OTHER_HOST = "cafe.example"
_OWNER = "user-maya"
_POCKET = "pk-foreign"
_CONTROL_POCKET = "pk-foreign-control"
_OWNED_POCKET = {"name": "Brew Co", "rippleSpec": {}}

# Off the catalog, not written as a number: a test that hard-codes 19 stops
# testing the purchase and starts testing the sticker.
_STAFF = site_plans.site_scoped_tier("staff")
assert _STAFF is not None
_PRICE_CREDITS = _STAFF.monthly_price_usd * 100
_FUNDED = _PRICE_CREDITS * 5


class _FakeMembership:
    def __init__(self, workspace: str, role: str) -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    def __init__(self, workspace_id: str, user_id: str, role: str) -> None:
        self.id = user_id
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace=workspace_id, role=role)]


def _build_app(workspace_id: str, *, role: str = "admin", user_id: str = _OWNER) -> FastAPI:
    """Mount the sites router with one authenticated principal.

    ``role`` is the whole point of the parameter: ``sites.buy_plan`` sits at ADMIN
    and resolves off the membership, so a "member" app is how the money gate is
    exercised rather than described.

    No plan patch here — the tree's conftest owns ``get_workspace_plan`` for every
    test in ``tests/ee/sites/`` and a second patch on that target unwinds in the
    wrong order (see the long note in ``test_router.py``).
    """
    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.sites.router import router as sites_router

    fake_user = _FakeUser(workspace_id, user_id, role)
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


def _url(pocket_id: str, suffix: str = "") -> str:
    return f"/api/v1/sites/by-pocket/{pocket_id}/foreign-concierge{suffix}"


@pytest.fixture(autouse=True)
def _no_background_knowledge_sync(monkeypatch):
    """Close the background KB sync the bind schedules instead of running it.

    For a FOREIGN site that sync CRAWLS the customer's origin, so leaving it armed
    makes every test in this file attempt a real outbound request to
    ``brewco.example`` on a detached task the event loop then tears down
    mid-flight. Grounding has its own suites (tests/ee/sites/test_foreign_grounding.py,
    tests/cloud/test_site_foreign_grounding.py); this file measures the HTTP
    surface and must not depend on the network to do it.
    """
    from pocketpaw_ee.sites import kb_ingest

    def _close_without_running(coro: Any) -> None:
        coro.close()

    monkeypatch.setattr(kb_ingest, "_default_sync_scheduler", _close_without_running)


@pytest_asyncio.fixture
async def store(tmp_path, beanie_test_db):  # noqa: ARG001 — beanie_test_db initialises Beanie
    """A tmp paw-bar store patched in at the source, so the provisioning funnel the
    bind runs and the assertions below read the SAME widget table."""
    s = PawBarStore(tmp_path / "foreign-endpoints.db")
    with patch("pocketpaw_ee.api.get_paw_bar_store", return_value=s):
        yield s


async def _verify_origin(workspace_id: str, host: str = _HOST, *, age_days: int = 0) -> None:
    """Seed the VERIFIED ownership claim the bind gates on.

    ``age_days`` backdates ``verified_at``, which is the only input the freshness
    rule reads — 31 days is a proof the bind must refuse while the row still says
    "verified", which is exactly the state a stale gate exists for.

    Written directly rather than driven through the probe: this file is about the
    HTTP surface, and tests/ee/sites/test_domain_ownership.py owns how a proof is
    obtained.
    """
    now = datetime.now(UTC)
    await SiteOriginClaim(
        workspace=workspace_id,
        host=host,
        token=f"pawverify-{uuid4().hex}",
        status="verified",
        issued_at=now - timedelta(days=age_days),
        expires_at=now + timedelta(days=7),
        verified_at=now - timedelta(days=age_days),
        method="well-known",
        issued_by=_OWNER,
    ).insert()


async def _fund(workspace_id: str, credits: int = _FUNDED) -> None:
    await credits_service.grant(
        workspace=workspace_id,
        amount=credits,
        cause="top_up",
        idempotency_key=f"seed-{workspace_id}-{credits}-{uuid4().hex[:6]}",
    )


def _owned_pocket(workspace_id: str):
    """Patch the pocket ownership gate to a pocket this caller owns in
    ``workspace_id``.

    The gate itself is covered in tests/cloud/sites/test_foreign_site_mint.py; this
    file is about what the endpoint does once it passes. The ``workspace`` key is
    why the parameter exists: ``pockets_service.get`` denies only a PRIVATE
    pocket, so the mint compares that key against the minting tenant, and a doc
    without it would make every request here the cross-tenant refusal.
    """
    return patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(return_value={**_OWNED_POCKET, "workspace": workspace_id}),
    )


async def _foreign_rows(workspace_id: str, pocket_id: str = _POCKET) -> list[Site]:
    return await Site.find(
        {"workspace": workspace_id, "pocket_id": pocket_id, "foreign_origin": True}
    ).to_list()


async def _bind(
    client: AsyncClient, workspace_id: str, pocket_id: str = _POCKET, host: str = _HOST
) -> Any:
    with _owned_pocket(workspace_id):
        return await client.post(
            _url(pocket_id), json={"allowed_origins": [f"https://{host}"], "name": "Brew Co"}
        )


async def _read(client: AsyncClient, workspace_id: str, pocket_id: str = _POCKET) -> Any:
    """GET the concierge with the pocket gate satisfied.

    Every route on this surface now asks ``pockets_service.get`` before it touches
    a Site, so a test that called the route bare would be exercising the refusal
    rather than the read. The refusals get their own tests, which patch this same
    target to deny.
    """
    with _owned_pocket(workspace_id):
        return await client.get(_url(pocket_id))


async def _rotate(client: AsyncClient, workspace_id: str, pocket_id: str = _POCKET) -> Any:
    with _owned_pocket(workspace_id):
        return await client.post(_url(pocket_id, "/rotate-key"))


async def _rebind(
    client: AsyncClient, workspace_id: str, body: dict, pocket_id: str = _POCKET
) -> Any:
    with _owned_pocket(workspace_id):
        return await client.post(_url(pocket_id, "/rebind"), json=body)


async def _arm_the_trap(client: AsyncClient, workspace_id: str) -> int:
    """Bind a CONTROL pocket and prove the two assertions below can move.

    Returns the balance after the control purchase. Without this, "no row was
    written and no credits were spent" is the same sentence a test whose request
    404'd would produce.
    """
    resp = await _bind(client, workspace_id, _CONTROL_POCKET)
    assert resp.status_code == 200, resp.text
    assert len(await _foreign_rows(workspace_id, _CONTROL_POCKET)) == 1, (
        "the control bind must leave a row, or the row assertions below prove nothing"
    )
    balance = await credits_service.balance(workspace_id)
    assert balance == _FUNDED - _PRICE_CREDITS, (
        "the control bind must move the wallet, or the balance assertions prove nothing"
    )
    return balance


# --------------------------------------------------------------------------- #
# 1-2. The money, through the route
# --------------------------------------------------------------------------- #


async def test_a_bind_through_the_route_charges_once_and_returns_the_snippet(store):
    """The feature: one POST leaves a paid concierge and the snippet to paste.

    The snippet rather than the key, because the snippet is what the owner does
    something with — and it is empty unless the bind also provisioned a bar with
    an agent behind it, so asserting on it covers the funnel the route triggers.
    """
    ws = "ws-ep-first"
    await _fund(ws)
    await _verify_origin(ws)

    app = _build_app(ws)
    async with _client(app) as c:
        resp = await _bind(c, ws)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["exists"] is True
    assert body["site_id"]
    assert body["subscription_status"] == "active", "the month was bought"
    assert body["plan_tier"] == _STAFF.key
    assert body["site_key"].startswith("site_key_")
    assert body["site_key"] in body["embed_snippet"], "the snippet must carry this site's key"
    assert "data-paw-bar-embed" in body["embed_snippet"]
    assert body["agent_id"], "the bind must leave the bar bound to a concierge agent"
    assert len(body["origins"]) == 1
    assert body["origins"][0]["host"] == _HOST
    assert body["origins"][0]["verified"] is True
    assert body["origins"][0]["verification_fresh"] is True
    assert body["origins"][0]["verified_at"], "the panel needs the date to say 'expires in N days'"
    assert await credits_service.balance(ws) == _FUNDED - _PRICE_CREDITS

    widgets = await store.list_widgets(pocket_id=_POCKET, workspace_id=ws, limit=10)
    assert len(widgets) == 1


async def test_a_second_bind_through_the_route_charges_nothing_more(store):  # noqa: ARG001
    """THE PROPERTY THE ROUTE CAN LOSE ON ITS OWN.

    The service's idempotence is a derived primary key plus a lock, and neither
    helps if the endpoint calls ``mint_foreign_site`` — the always-mints primitive
    — instead of ``bind_foreign_concierge``. Both take the same keyword arguments,
    so that substitution compiles, answers 200, returns a perfectly good concierge
    and charges a second $19. Only the wallet and the row count can see it.
    """
    ws = "ws-ep-twice"
    await _fund(ws)
    await _verify_origin(ws)

    app = _build_app(ws)
    async with _client(app) as c:
        first = await _bind(c, ws)
        assert first.status_code == 200, first.text
        after_first = await credits_service.balance(ws)

        second = await _bind(c, ws)

    assert second.status_code == 200, second.text
    assert second.json()["site_id"] == first.json()["site_id"], "the same concierge"
    assert len(await _foreign_rows(ws)) == 1, "one concierge per pocket"
    assert after_first == _FUNDED - _PRICE_CREDITS, "the first bind charged"
    assert await credits_service.balance(ws) == after_first, "and the second charged nothing"


# --------------------------------------------------------------------------- #
# 3-4. The verification gates, and that they stay different problems
# --------------------------------------------------------------------------- #


async def test_an_unverified_origin_is_refused_with_no_row_and_no_charge(store):  # noqa: ARG001
    """A domain nobody proved they own must not become a live embed.

    The CODE is asserted, not only the 403: delete the unverified arm and the
    freshness arm refuses in its place (``verification_is_fresh(None)`` is False),
    so a status-only assertion is green against a deleted gate.
    """
    ws = "ws-ep-unverified"
    await _fund(ws)
    await _verify_origin(ws)  # the CONTROL pocket's origin, not the refused one

    app = _build_app(ws)
    async with _client(app) as c:
        armed_balance = await _arm_the_trap(c, ws)
        resp = await _bind(c, ws, _POCKET, _OTHER_HOST)

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "sites.origin_unverified"
    assert await _foreign_rows(ws) == [], "a refused bind must leave no row"
    assert await credits_service.balance(ws) == armed_balance, "and must not charge"


async def test_a_stale_verification_is_refused_distinctly_from_an_unverified_one(store):  # noqa: ARG001
    """A proof of control is a fact with a DATE, not a permanent licence.

    31 days on a row that still reads ``status: verified``. It must refuse — the
    crawl that grounds this concierge applies the same 30-day rule, so binding on
    a stale proof sells a bar that can never learn anything — and it must refuse
    with its OWN code, because "re-verify your domain" and "you never claimed this
    domain" are different instructions and the panel has to pick one.
    """
    ws = "ws-ep-stale"
    await _fund(ws)
    await _verify_origin(ws)
    await _verify_origin(ws, _OTHER_HOST, age_days=31)

    app = _build_app(ws)
    async with _client(app) as c:
        armed_balance = await _arm_the_trap(c, ws)
        resp = await _bind(c, ws, _POCKET, _OTHER_HOST)

    assert resp.status_code == 403, resp.text
    code = resp.json()["error"]["code"]
    assert code == "sites.origin_verification_stale"
    assert code != "sites.origin_unverified", "the two refusals must stay distinguishable"
    assert await _foreign_rows(ws) == [], "a refused bind must leave no row"
    assert await credits_service.balance(ws) == armed_balance, "and must not charge"


async def test_the_read_reports_a_stale_origin_as_verified_but_not_fresh(store):  # noqa: ARG001
    """The panel needs the three states, not a boolean.

    A concierge bound while its proof was fresh keeps working and keeps its row,
    so ``verified`` stays true — and the next grounding run will refuse it. The
    read says both, which is the only way the owner can be told to re-verify
    before the concierge quietly stops learning.
    """
    ws = "ws-ep-read-stale"
    await _fund(ws)
    await _verify_origin(ws)

    app = _build_app(ws)
    async with _client(app) as c:
        bound = await _bind(c, ws)
        assert bound.status_code == 200, bound.text

        # Age the proof AFTER the purchase, which is what a month passing does.
        claim = await SiteOriginClaim.find_one(
            SiteOriginClaim.workspace == ws, SiteOriginClaim.host == _HOST
        )
        assert claim is not None
        claim.verified_at = datetime.now(UTC) - timedelta(days=31)
        await claim.save()

        resp = await _read(c, ws)

    assert resp.status_code == 200, resp.text
    origin = resp.json()["origins"][0]
    assert origin["host"] == _HOST
    assert origin["verified"] is True
    assert origin["verification_fresh"] is False


# --------------------------------------------------------------------------- #
# 4. Authorization, and the wallet
# --------------------------------------------------------------------------- #


async def test_a_member_who_may_not_buy_a_plan_cannot_bind_a_concierge(store):  # noqa: ARG001
    """A bind is a recurring charge, so it needs the same role a paid publish does.

    ``sites.buy_plan`` sits at ADMIN with the refusal code
    ``sites.plan_purchase_forbidden`` — the hole it was written to close was a
    member committing the company to a monthly bill, which is precisely what this
    endpoint does. The admin app in the same test is the armed trap: it proves the
    request shape reaches the service and buys a concierge.
    """
    ws = "ws-ep-role"
    await _fund(ws)
    await _verify_origin(ws)

    admin = _build_app(ws, role="admin")
    async with _client(admin) as c:
        armed_balance = await _arm_the_trap(c, ws)

    member = _build_app(ws, role="member", user_id="user-sam")
    async with _client(member) as c:
        resp = await _bind(c, ws)

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "sites.plan_purchase_forbidden"
    assert await _foreign_rows(ws) == [], "a refused caller must leave no row"
    assert await credits_service.balance(ws) == armed_balance, "and must not charge"


async def test_a_member_can_still_read_whether_a_concierge_exists(store):  # noqa: ARG001
    """The read is ``fabric.read``, deliberately.

    Someone who cannot commit the workspace to a charge still has to be able to
    see that the concierge exists and which of its origins have gone stale;
    hiding that behind the buy gate would make the panel unreadable for most of a
    team.
    """
    ws = "ws-ep-member-read"
    await _fund(ws)
    await _verify_origin(ws)

    admin = _build_app(ws, role="admin")
    async with _client(admin) as c:
        bound = await _bind(c, ws)
        assert bound.status_code == 200, bound.text

    member = _build_app(ws, role="member", user_id="user-sam")
    async with _client(member) as c:
        resp = await _read(c, ws)

    assert resp.status_code == 200, resp.text
    assert resp.json()["site_id"] == bound.json()["site_id"]


async def test_a_wallet_that_cannot_cover_the_month_gets_the_established_402(store):  # noqa: ARG001
    """A short wallet is ``credits.insufficient`` (402) and leaves nothing behind.

    The row is inserted UNPAID before the debit — it exists so the charge has a
    stable ``site_id`` to key on — so "no row survives" is the assertion that
    covers the rollback rather than the refusal.
    """
    ws = "ws-ep-broke"
    await _verify_origin(ws)

    app = _build_app(ws)
    async with _client(app) as c:
        resp = await _bind(c, ws)

    assert resp.status_code == 402, resp.text
    assert resp.json()["error"]["code"] == "credits.insufficient"
    assert await _foreign_rows(ws) == [], "an unpaid row must not survive the 402"


# --------------------------------------------------------------------------- #
# 5. Rotate and rebind
# --------------------------------------------------------------------------- #


async def test_rotating_through_the_route_retires_the_old_key_at_the_resolver(store):  # noqa: ARG001
    """Rotation exists to make a LEAKED key stop working, so ask the resolver.

    A field-shaped assertion ("the response carries a different key") passes for a
    version that mints a key, renders it into the snippet and never saves it.
    """
    from fastapi import HTTPException
    from pocketpaw_ee.cloud.auth import site_keys

    ws = "ws-ep-rotate"
    await _fund(ws)
    await _verify_origin(ws)

    app = _build_app(ws)
    async with _client(app) as c:
        bound = await _bind(c, ws)
        assert bound.status_code == 200, bound.text
        old_key = bound.json()["site_key"]

        resp = await _rotate(c, ws)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["site_key"] != old_key
    assert body["site_id"] == bound.json()["site_id"], "a rotation is not a repurchase"
    assert body["subscription_status"] == "active"
    assert body["site_key"] in body["embed_snippet"], "the new snippet must carry the new key"

    with pytest.raises(HTTPException) as exc:
        await site_keys.resolve_site_key(old_key, f"https://{_HOST}", "cust-1")
    assert exc.value.status_code == 401, "the leaked key must no longer resolve"

    ctx = await site_keys.resolve_site_key(body["site_key"], f"https://{_HOST}", "cust-1")
    assert ctx is not None, "and the new one must"


async def test_rotating_a_pocket_with_no_concierge_is_a_404(store):  # noqa: ARG001
    """Nothing to rotate is a real failure, unlike nothing to read."""
    ws = "ws-ep-rotate-404"
    app = _build_app(ws)
    async with _client(app) as c:
        resp = await _rotate(c, ws)

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "site.not_found"


async def test_rebinding_through_the_route_reprovisions_the_bar(store):
    """An empty ``agent_id`` is the repair path, not a no-op.

    It clears the stale bind and lets the funnel resolve-or-mint the canonical
    agent again, which is what an owner needs after deleting the agent their bar
    pointed at. The credential is untouched either way — a rebind that re-minted
    the key would silently 403 every visitor on the buyer's live page.

    A rebind to ANOTHER TENANT's agent is refused inside the funnel and is
    covered at that level (tests/cloud/sites/test_foreign_concierge_bind.py).
    """
    ws = "ws-ep-rebind"
    await _fund(ws)
    await _verify_origin(ws)

    app = _build_app(ws)
    async with _client(app) as c:
        bound = await _bind(c, ws)
        assert bound.status_code == 200, bound.text
        key_before = bound.json()["site_key"]
        widget_id = bound.json()["widget_id"]
        assert widget_id

        await store.update_fields(widget_id, {"agent_id": ""}, workspace_id=ws)
        cleared = await _read(c, ws)
        assert cleared.json()["agent_id"] == "", "the trap is armed: the bar has no agent"

        resp = await _rebind(c, ws, {})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_id"], "the rebind must leave the bar bound again"
    assert body["site_key"] == key_before, "and must not touch the credential"


async def test_the_read_reports_no_concierge_before_a_bind(store):  # noqa: ARG001
    """``exists: false``, not a 404 — the normal first state of the setup panel."""
    ws = "ws-ep-empty"
    app = _build_app(ws)
    async with _client(app) as c:
        resp = await _read(c, ws)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["exists"] is False
    assert body["pocket_id"] == _POCKET
    assert body["site_id"] == ""
    assert body["embed_snippet"] == ""


async def test_the_read_never_returns_the_pockets_published_site(store):  # noqa: ARG001
    """A pocket can have BOTH a published Worker site and a foreign concierge.

    The lookup filters on ``foreign_origin``, so reporting the published row here
    would hand the panel a rotate button that invalidates the embed key of a site
    somebody is actually serving.
    """
    ws = "ws-ep-both"
    published = Site(
        workspace=ws,
        pocket_id=_POCKET,
        owner=_OWNER,
        name="Published",
        script_name="pk-foreign-live",
        deployed=True,
        signed_key="site_key_published",
    )
    await published.insert()

    app = _build_app(ws)
    async with _client(app) as c:
        resp = await _read(c, ws)

    assert resp.status_code == 200, resp.text
    assert resp.json()["exists"] is False, "the published row is not this pocket's concierge"


# --------------------------------------------------------------------------- #
# The wiring itself
# --------------------------------------------------------------------------- #


async def test_the_four_routes_are_registered_with_the_gates_they_need():
    """Read off the router rather than an app, so it needs no DB and no license.

    It proves the wiring a request-level test cannot: that the money gate is on
    the bind and NOT on the read. A version with ``sites.buy_plan`` missing from
    the POST still passes every happy-path test in this file, because the app
    those tests build authenticates an admin.

    The guard is a closure over the action name, so the name is read out of its
    cells — its repr is just a function address.
    """
    from pocketpaw_ee.sites.router import router

    base = "/sites/by-pocket/{pocket_id}/foreign-concierge"
    # Keyed on (path, METHOD), not on path: the bind and the read share one path
    # and are two route objects, so a path-keyed dict silently keeps whichever was
    # registered last and every assertion below would be about the GET.
    routes = {
        (route.path, method): route
        for route in router.routes
        if "foreign-concierge" in getattr(route, "path", "")
        for method in getattr(route, "methods", set())
    }
    assert set(routes) == {
        (base, "POST"),
        (base, "GET"),
        (f"{base}/rotate-key", "POST"),
        (f"{base}/rebind", "POST"),
    }

    def _actions(route: Any) -> set[str]:
        return {
            cell.cell_contents
            for sub in route.dependant.dependencies
            for cell in (sub.call.__closure__ or ())
            if isinstance(cell.cell_contents, str)
        }

    bind_actions = _actions(routes[(base, "POST")])
    assert "fabric.write" in bind_actions, "the surface gate every sibling mutation carries"
    assert "sites.buy_plan" in bind_actions, "and the money gate, because a first bind buys"

    read_actions = _actions(routes[(base, "GET")])
    assert "fabric.read" in read_actions
    assert "sites.buy_plan" not in read_actions, "a member must be able to read the panel"

    for suffix in ("/rotate-key", "/rebind"):
        actions = _actions(routes[(f"{base}{suffix}", "POST")])
        assert "fabric.write" in actions, suffix
        # Neither spends money: a rotation is not a repurchase and a rebind leaves
        # the tier bought. Requiring an admin for a leak response would leave the
        # member who can see the leak unable to act on it.
        assert "sites.buy_plan" not in actions, suffix


# --------------------------------------------------------------------------- #
# 6. The pocket gate on read, rotate and rebind
#
# All three resolve through ``foreign_site_for_pocket``, which filters on
# workspace and ``foreign_origin`` and nothing else, and both surface guards are
# MEMBER (``fabric.read`` / ``fabric.write`` describe what a call INTENDS, not who
# may make it). So a member refused a PRIVATE pocket everywhere else in the
# product could still read its concierge's credential and rotate it off a live
# page.
#
# EACH TEST ASSERTS THE HARM, NOT THE STATUS. A 403 is also what a route with a
# typo'd path returns to this client, and three routes carrying the same one-line
# check is exactly the shape where a single test reads like coverage for all of
# them. So every test here drives the ALLOWED call first — proving the thing it is
# about to assert can move — and then asserts the refused call left the key, the
# agent or the wallet where the allowed one left it.
# --------------------------------------------------------------------------- #


def _denied_pocket():
    """``pockets_service.get`` as it behaves for a pocket this caller may not open.

    The real refusal, not an invented one: ``Forbidden("pocket.access_denied")``
    is what that service raises for a private pocket outside the caller's reach.
    """
    return patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(side_effect=Forbidden("pocket.access_denied", "no access")),
    )


async def test_the_read_refuses_a_pocket_the_caller_cannot_open(store):  # noqa: ARG001
    """The credential is in the body, so the pocket gate has to be on the read."""
    ws = "ws-ep-read-denied"
    await _fund(ws)
    await _verify_origin(ws)

    app = _build_app(ws)
    async with _client(app) as c:
        bound = await _bind(c, ws)
        assert bound.status_code == 200, bound.text
        allowed = await _read(c, ws)
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["site_key"], (
            "the allowed read must hand back a key, or the refusal below proves nothing"
        )

        with _denied_pocket():
            resp = await c.get(_url(_POCKET))

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "pocket.access_denied"
    assert "site_key" not in resp.text


async def test_rotate_refuses_a_pocket_the_caller_cannot_open_and_leaves_the_key_live(store):  # noqa: ARG001
    """The loudest of the three: a rotation takes the customer's published page
    offline the instant it lands, so the refusal is asserted at the RESOLVER —
    the old key still opening a concierge session is the only proof nothing was
    retired."""
    from pocketpaw_ee.cloud.auth import site_keys

    ws = "ws-ep-rotate-denied"
    await _fund(ws)
    await _verify_origin(ws)

    app = _build_app(ws)
    async with _client(app) as c:
        bound = await _bind(c, ws)
        assert bound.status_code == 200, bound.text
        key = bound.json()["site_key"]

        with _denied_pocket():
            resp = await c.post(_url(_POCKET, "/rotate-key"))

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "pocket.access_denied"
    ctx = await site_keys.resolve_site_key(key, f"https://{_HOST}", "cust-1")
    assert ctx is not None, "the refused rotation must not have retired the live key"


async def test_rebind_refuses_a_pocket_the_caller_cannot_open_and_leaves_the_bar_bound(store):
    """Asserted on the WIDGET rather than the response: a rebind that ran and
    then 403'd would answer identically to one that never ran."""
    ws = "ws-ep-rebind-denied"
    await _fund(ws)
    await _verify_origin(ws)

    app = _build_app(ws)
    async with _client(app) as c:
        bound = await _bind(c, ws)
        assert bound.status_code == 200, bound.text
        widget_id = bound.json()["widget_id"]
        assert widget_id
        agent_before = bound.json()["agent_id"]
        assert agent_before, "the bar must start bound, or the assertion below cannot move"

        with _denied_pocket():
            resp = await c.post(_url(_POCKET, "/rebind"), json={})

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "pocket.access_denied"
    widget = await store.get_widget(widget_id, workspace_id=ws)
    assert widget is not None and widget.agent_id == agent_before


async def test_resolving_an_existing_concierge_refuses_a_pocket_the_caller_cannot_open(store):  # noqa: ARG001
    """The bind's RESOLVE arm, which never reaches the mint's gate.

    A second bind returns the existing row untouched — and that row carries the
    ``signed_key``. The arm is admin-gated, but an admin is not automatically a
    reader of every private pocket in the workspace, so the credential would be
    one repeat call away.
    """
    ws = "ws-ep-resolve-denied"
    await _fund(ws)
    await _verify_origin(ws)

    app = _build_app(ws)
    async with _client(app) as c:
        first = await _bind(c, ws)
        assert first.status_code == 200, first.text
        spent = await credits_service.balance(ws)

        with _denied_pocket():
            resp = await c.post(
                _url(_POCKET),
                json={"allowed_origins": [f"https://{_HOST}"], "name": "Brew Co"},
            )

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "pocket.access_denied"
    assert "site_key" not in resp.text
    assert await credits_service.balance(ws) == spent, "a refused resolve must not charge either"
    assert len(await _foreign_rows(ws)) == 1


async def test_the_route_passes_the_CALLERS_role_to_the_rebind_rule(store):
    """The wiring, which the service tests cannot see.

    The publish rule lives in the service and is relaxed for an ADMIN. If this
    route hard-coded that flag either way, every service test would still pass
    while the product had no rule at all (True) or no escape hatch (False). So
    the same rebind is driven twice over HTTP, as a member and as an admin, and
    only the roles differ.

    ``fabric.write`` is MEMBER, so both callers are past the surface guard — the
    difference below is the rule, not the gate.
    """
    from pocketpaw_ee.cloud.agents import service as agents_service
    from pocketpaw_ee.cloud.agents.dto import CreateAgentRequest

    ws = "ws-ep-rebind-role"
    await _fund(ws)
    await _verify_origin(ws)

    admin_app = _build_app(ws, role="admin")
    async with _client(admin_app) as c:
        bound = await _bind(c, ws)
        assert bound.status_code == 200, bound.text

    agent = await agents_service.create(
        agents_service.legacy_ctx(_OWNER, ws),
        ws,
        CreateAgentRequest(name="internal-hr", slug="internal-hr", visibility="workspace"),
    )

    member_app = _build_app(ws, role="member", user_id="user-sam")
    async with _client(member_app) as c:
        refused = await _rebind(c, ws, {"agent_id": agent.id})

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "sites.agent_not_published"

    async with _client(admin_app) as c:
        allowed = await _rebind(c, ws, {"agent_id": agent.id})

    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["agent_id"] == agent.id


async def test_the_read_is_a_404_for_a_pocket_that_does_not_exist(store):  # noqa: ARG001
    """``exists: false`` is about a pocket that HAS no concierge. A pocket that
    does not exist is a different sentence, and the pocket gate now runs first,
    so this route answers it the way every other pocket-scoped route does.

    Driven with NO patch on the pockets service, which is what makes it the real
    lookup: the pocket genuinely is not there.
    """
    ws = "ws-ep-no-pocket"
    app = _build_app(ws)
    async with _client(app) as c:
        resp = await c.get(_url("pk-does-not-exist"))

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "pocket.not_found"
